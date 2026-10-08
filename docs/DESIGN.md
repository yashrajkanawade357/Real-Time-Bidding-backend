# Design notes

## The problem

Three people bid on the same lot within a millisecond of each other. The
current price is 1,000. One bids 1,100, one bids 1,200, one bids 1,050. The
system must end up with **1,200** as the high bid. The 1,050 bid must be
rejected, and any bid that is too low *by the time it is processed* must be
rejected too. Everyone watching must see the change immediately. Someone
who refreshes, or whose wifi drops, must come back to exactly the same state.

The naive implementation does this:

```python
auction = SELECT current_price ...      # all three read 1,000
if amount > auction.current_price:      # all three pass
    UPDATE auctions SET current_price = amount   # last write wins
```

Whichever `UPDATE` happens to land last wins, even if it is the 1,050. That is
the bug `scripts/race_demo.py --unsafe` reproduces: with 300 simultaneous bids,
223 bidders were told "accepted", the final price was 369 even though 399 was
bid, and a lower bid overwrote a higher one 106 times.

## Decision 1: Lock the auction row for the whole bid

Every bid runs as **one transaction** that starts with
`SELECT … FROM auctions WHERE id = $1 FOR UPDATE`.

`FOR UPDATE` takes a row-level lock. A second transaction that wants the same
row waits until the first commits, then reads the **new** value. So bids on
one auction are processed strictly one at a time, in lock order. The rule
check (`amount >= current_price + min_increment`) always runs against
committed reality, never against what the client saw.

- Arrival order stops mattering. Whatever order 1,100 / 1,200 / 1,050 are
  processed in, the result is 1,200. If 1,200 goes first, the other two fail
  the check. If 1,050 goes first, it is accepted and then overtaken by the
  later bids.
- This also covers **stale** bids. A client that bids 1,100 because it last
  saw 1,000 is judged against the current price under the lock, and rejected
  if the price has moved on.
- Bids on **different** auctions lock different rows, so they don't block
  each other.
- **No deadlocks are possible.** Each transaction locks exactly one row.

### Alternatives I considered

| Option | Why not |
|---|---|
| **Last write wins** (no lock) | Wrong. See the race demo. |
| **An in-process lock** (`asyncio.Lock`, a mutex) | Only works inside one process. The moment there are two API instances behind a load balancer, there are two locks and the race is back. |
| **Redis / distributed lock** | A second system that must agree with the database. Lock expiry and data can drift apart (a lock times out while a slow write is still in flight). The database lock lasts exactly as long as the transaction and needs no extra infrastructure. |
| **`SERIALIZABLE` isolation** | Also correct, but under contention Postgres aborts the losers with serialization failures and every client has to retry. On a hot auction that is most bids. An explicit lock just queues them. |
| **Optimistic: a conditional `UPDATE`** | Also correct, and the closest relative of this design: `UPDATE auctions SET current_price=$2 … WHERE id=$1 AND status='open' AND $2 >= coalesce(current_price + min_increment, starting_price)`. If 0 rows change, the bid is rejected. I chose `FOR UPDATE` because a bid writes several things atomically (the bid log row, the auction row, the idempotency check, the notification) and needs a precise rejection reason. A single conditional statement can't give "too low" vs "closed" vs "duplicate" as cleanly. This conditional-write pattern is exactly what DynamoDB's `ConditionExpression` does, which is how this maps to a serverless AWS design (below). |

## Decision 2: The database is the only source of truth

The server keeps **no auction state in memory**: no cached price and no
"current leader" variable. The only in-memory structure is the list of open
sockets per auction (`app/hub.py`).

- A fresh page load, a reconnect, or a request to a brand-new server process
  all read the same rows. `test_state_survives_a_server_restart` stops the
  server entirely and starts a new one.
- A client disconnecting can only ever remove a socket from a set. There is
  no shared state it could leave half-updated.
- Any number of API instances can run side by side.

## Decision 3: LISTEN/NOTIFY to fan out changes

When a bid is accepted, the same transaction calls
`pg_notify('auction_events', <json>)`. Every API instance holds one
connection that `LISTEN`s on that channel, and pushes the event to its own
connected sockets.

Why Postgres's own pub/sub instead of Redis, SNS or Kafka:

- **It is transactional.** Postgres delivers a NOTIFY only when the
  transaction commits. A bid that rolls back is never announced, so no client
  ever sees a price that doesn't exist.
- **It arrives in commit order**, which is the same order the lock granted.
- **It crosses instances.** A client on instance B hears about a bid placed
  on instance A (`test_two_instances_share_live_updates`). The load balancer
  needs no sticky sessions.
- **No extra infrastructure.** That matters for a single-region system of this
  size. At much larger fan-out I would move to a dedicated broker.

Its limits, and how I handle them:

- NOTIFY is **not durable**. If an instance's LISTEN connection drops, events
  during the gap are lost to it. So after every reconnect the listener pushes
  a **fresh snapshot** to every client on that instance
  (`test_lost_event_feed_is_followed_by_a_fresh_snapshot`). Clients may get a
  redundant snapshot, but they never stay stale.
- Payloads are capped at 8 KB. Each event is a few hundred bytes.

## Decision 4: Subscribe, then snapshot, then use versions

When a socket connects, the server:

1. adds it to the auction's broadcast set, **then**
2. reads a snapshot from the database and sends it.

If the order were reversed, a bid committed between "read snapshot" and
"subscribe" would be missed forever. In this order, any such event is queued
behind the snapshot.

Every auction row has a `version` that increases with each change, and every
snapshot and event carries it. The client keeps the highest version it has
seen and **ignores anything older or equal**. A queued event that the snapshot
already includes is dropped, and a newer one is applied. A snapshot and the
live feed can never be applied out of order.

The snapshot itself is read in one `REPEATABLE READ` transaction, so the bid
list and the auction row always describe the same version.

## Decision 5: Idempotency keys for retries

A client that sends a bid and loses its connection before the reply **cannot
know** whether the bid committed. The only safe move is to retry, and a retry
must not bid twice.

Every bid carries a client-generated `request_id`. The bids table has
`UNIQUE (auction_id, request_id)`, and the bid transaction checks for an
existing row **after** taking the auction lock. A retry therefore returns the
original outcome with `duplicate: true` instead of creating a second bid.
Even 25 copies of the same request fired concurrently produce one row
(`test_concurrent_retries_of_one_request_create_one_bid`).

The browser client keeps unanswered bids in an outbox and resends them, with
the same ids, after every reconnect.

## Decision 6: Disconnects can't tear a bid in half

- The bid call is wrapped in `asyncio.shield`. If the socket dies mid-bid, the
  transaction still runs to COMMIT or ROLLBACK instead of being cancelled
  halfway. Postgres guarantees atomicity either way. The shield just avoids
  pointless rollbacks, and the client learns the outcome via its idempotent
  retry.
- Each socket has a **bounded send queue**. A slow client can't stall
  broadcasts to everyone else. When its queue fills, it is disconnected with
  code 4008 and gets a fresh snapshot on reconnect.
- On shutdown the server closes sockets with 1012 ("restarting"). Clients
  reconnect with **exponential backoff plus jitter**, so a restart isn't
  followed by every tab reconnecting in the same instant.
- uvicorn pings every socket every 20 s, so dead peers are noticed and
  cleaned up.

## Decision 7: Closing auctions exactly once

- **Time comes from the database (`now()`), not the app servers**, so clock
  skew between instances can't decide who got in before the deadline. Because
  `now()` is the transaction's start time, a bid that arrived before the
  deadline but waited for the lock still counts.
- A bid after `ends_at` is rejected even if the auction hasn't been marked
  closed yet.
- Every instance runs a closer every second:
  `UPDATE auctions SET status='closed' … WHERE status='open' AND ends_at <= now()`.
  Postgres re-checks the `WHERE` under the row lock, so when several instances
  race, exactly one flips each auction and announces the winner
  (`test_racing_closers_close_each_auction_exactly_once`).

## Decision 8: Small things that matter

- **Money is `BIGINT` whole units.** No floats anywhere.
- **Every bid attempt is stored**, rejected ones included, as an append-only
  audit log.
- **Migrations take a Postgres advisory lock**, so several instances booting
  at once apply each migration exactly once.
- **`lock_timeout = 5s`.** Under pathological contention a bid fails fast
  with "busy, retry" (HTTP 503) rather than hanging. The idempotency key makes
  that retry safe.

## What happens when…

| Situation | Outcome | Why state stays correct |
|---|---|---|
| Two bids in the same millisecond | One waits for the other's lock | Rules are checked under the lock |
| Two *equal* bids at once | First to the lock wins; the second is "too low" | The next bid must be ≥ price + increment (`test_equal_simultaneous_bids_only_one_wins`) |
| A bid based on an old price | Rejected `bid_too_low`, with the current minimum | Checked against the locked row, not the client's view |
| Socket drops mid-bid | Bid commits or rolls back as a whole | Shielded transaction; the retry with the same `request_id` returns the result |
| Client misses events while offline | Gets a snapshot on reconnect | Snapshot is read from Postgres |
| API process crashes mid-transaction | Postgres rolls it back; no NOTIFY sent | Atomic commit; notification is transactional |
| An API instance restarts | Clients get 1012 and reconnect (to any instance) | No state lives in the instance |
| The instance's LISTEN connection drops | It reconnects and resends snapshots | NOTIFY isn't durable, so resync covers the gap |
| Slow client | Disconnected with 4008, then resynced | Bounded queue protects other clients |
| Postgres is down | Bids fail with an error; nothing is half-written | There is no second copy of state to diverge |
| Many instances race to close an auction | Closed exactly once | Conditional `UPDATE … WHERE status='open'` |

## How this would map onto AWS

**As built** (container + managed Postgres): ECS Fargate behind an
Application Load Balancer, with RDS for PostgreSQL. The ALB supports
WebSockets natively and needs no stickiness. See [DEPLOY_AWS.md](DEPLOY_AWS.md).

**Fully serverless equivalent** (not built; how the same guarantees translate):

| Here | Serverless AWS |
|---|---|
| WebSocket server | API Gateway WebSocket API (`$connect`, `$disconnect`, `bid` routes → Lambda) |
| Row lock + rule check | DynamoDB `UpdateItem` with a `ConditionExpression` (`amount >= current_price + min_increment AND status = open`). A failed condition is a rejected bid |
| Bid log + idempotency | DynamoDB `TransactWriteItems` putting the bid item with `attribute_not_exists(request_id)` |
| LISTEN/NOTIFY fan-out | DynamoDB Streams → Lambda → `postToConnection` for each connection on the auction |
| Socket list per auction | A DynamoDB table of connection ids, written on `$connect` and deleted on `$disconnect` |
| Snapshot on connect | Lambda reads the auction item on `$connect` / first message |

## Measured

- 300 simultaneous bids on one auction: **0.50 s** end to end over HTTP,
  correct winner, accepted bids strictly increasing.
- The same 300 bids through the unsafe path: **wrong winner**, final price
  369 instead of 399, and **106** cases of a lower bid replacing a higher one.
- 25 automated tests against a real Postgres, all passing.

## FAQ

**Why not just put a unique constraint or a `CHECK` on the price?**
A constraint can't express "greater than whatever the price is at the moment
this bid commits". That depends on concurrent transactions, which is what the
lock serialises.

**Doesn't one lock per auction limit throughput?**
For one auction, yes, and that is inherent: an auction's bids must be totally
ordered. About 600 bids/s on one lot on a laptop. Different auctions proceed
in parallel. Very hot single items would be sharded by time or handled with
an in-memory sequencer that persists to a log, but that is far beyond this scope.

**What if two API instances get the same bid?**
They both queue on the same Postgres row lock. Where the request lands makes
no difference.

**Why WebSockets and not Server-Sent Events?**
Bids flow both ways on the same connection, with a per-bid acknowledgement
(`bid_result`). SSE is one-way, so bids would need a separate REST call. That
works too: the REST bid endpoint exists and is what the race demo uses.

**What would you add next?**
Authentication (the bidder identity should come from a verified token, not a
query parameter), rate limiting per bidder, proxy/max bids, and anti-sniping
extensions (extend `ends_at` when a bid lands in the final seconds, inside the
same locked transaction).
