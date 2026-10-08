"""The one place where a bid becomes - or does not become - the new high bid.

Why this is correct under concurrency:

1. Every bid runs in its own transaction and first takes the auction row's
   lock (SELECT ... FOR UPDATE). Bids on the same auction therefore execute
   one at a time, in lock order, however many arrive at once and however many
   API instances receive them. Bids on different auctions don't block each other.

2. The rules are checked against the row as it is *now*, under that lock -
   never against what the client last saw. A bid built from a stale view
   simply fails "must be at least current price + increment" and is rejected.
   This is not last-write-wins: a lower bid that arrives later can never
   overwrite a higher one.

3. The bid row, the new price and the change notification are written in the
   same transaction, so they commit together or not at all. Postgres delivers
   NOTIFY only after COMMIT, so nobody is ever told about a bid that rolled back.

4. Each bid carries a client-chosen request_id. A resend (say, after a dropped
   connection, when the client can't know whether the first attempt landed)
   returns the stored outcome instead of bidding twice.

5. Anti-sniping: a bid accepted in the last `soft_close` seconds pushes the
   end back to `soft_close` seconds after it. That happens in the same UPDATE,
   under the same lock, so the deadline every client sees always matches the
   bids that were actually accepted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.auctions import AUCTION_COLUMNS, BID_COLUMNS, auction_dict, bid_dict, min_next_bid
from app.events import CHANNEL

log = logging.getLogger(__name__)

TOO_LOW = "bid_too_low"
CLOSED = "auction_closed"


class AuctionNotFound(Exception):
    pass


class AuctionBusy(Exception):
    """The auction row stayed locked longer than lock_timeout. Safe to retry."""


@dataclass(frozen=True)
class BidOutcome:
    accepted: bool
    duplicate: bool
    reason: str | None
    bid: dict[str, Any] | None
    auction: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "reason": self.reason,
            "bid": self.bid,
            "auction": self.auction,
        }


def _rejection_reason(auction: asyncpg.Record, amount: int) -> str | None:
    # db_now is the transaction's start time, i.e. when the bid arrived - a bid
    # that arrived before the deadline but queued for the lock still counts.
    if (auction["status"] != "open" or auction["removed_at"] is not None
            or auction["db_now"] >= auction["ends_at"]):
        return CLOSED
    if amount < min_next_bid(auction):
        return TOO_LOW
    return None


async def _notify(conn: asyncpg.Connection, event: dict[str, Any]) -> None:
    await conn.execute("SELECT pg_notify($1, $2)", CHANNEL, json.dumps(event))


async def place_bid(
    pool: asyncpg.Pool, auction_id: int, bidder: str, amount: int, request_id: str,
    *, user_id: int | None = None, soft_close: int = 0,
) -> BidOutcome:
    try:
        async with pool.acquire() as conn, conn.transaction():
            # The lock. Every concurrent bid on this auction queues here.
            auction = await conn.fetchrow(
                f"SELECT {AUCTION_COLUMNS}, now() AS db_now FROM auctions WHERE id = $1 FOR UPDATE",
                auction_id,
            )
            if auction is None:
                raise AuctionNotFound(auction_id)

            # Checked after taking the lock, so two copies of the same request
            # racing each other are serialised too: the second one sees the first.
            previous = await conn.fetchrow(
                f"SELECT {BID_COLUMNS} FROM bids WHERE auction_id = $1 AND request_id = $2",
                auction_id,
                request_id,
            )
            if previous is not None:
                return BidOutcome(
                    accepted=previous["status"] == "accepted",
                    duplicate=True,
                    reason=previous["reason"],
                    bid=bid_dict(previous, private=True),
                    auction=auction_dict(auction),
                )

            reason = _rejection_reason(auction, amount)
            bid = await conn.fetchrow(
                f"""
                INSERT INTO bids (auction_id, bidder, amount, request_id, status, reason, user_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING {BID_COLUMNS}
                """,
                auction_id,
                bidder,
                amount,
                request_id,
                "rejected" if reason else "accepted",
                reason,
                user_id,
            )
            if reason is not None:
                # Recorded for the audit trail; the auction itself is untouched.
                return BidOutcome(False, False, reason, bid_dict(bid, private=True), auction_dict(auction))

            # now() is when this bid's transaction began. If that's inside the
            # soft-close window, the auction now ends soft_close seconds later.
            updated = await conn.fetchrow(
                f"""
                UPDATE auctions
                SET current_price = $2, leader = $3,
                    bid_count = bid_count + 1, version = version + 1,
                    ends_at = CASE WHEN $4::int > 0 AND ends_at - now() < make_interval(secs => $4::int)
                                   THEN now() + make_interval(secs => $4::int) ELSE ends_at END,
                    extensions = extensions + CASE WHEN $4::int > 0 AND ends_at - now() < make_interval(secs => $4::int)
                                                   THEN 1 ELSE 0 END
                WHERE id = $1
                RETURNING {AUCTION_COLUMNS}
                """,
                auction_id,
                amount,
                bidder,
                soft_close,
            )
            auction_out = auction_dict(updated)
            # Everyone hears about the bid; only the bidder gets its request_id back.
            await _notify(conn, {"type": "bid_accepted", "auction": auction_out, "bid": bid_dict(bid)})
            return BidOutcome(True, False, None, bid_dict(bid, private=True), auction_out)
    except asyncpg.exceptions.LockNotAvailableError as exc:
        raise AuctionBusy(auction_id) from exc


async def place_bid_unsafe(
    pool: asyncpg.Pool, auction_id: int, bidder: str, amount: int, delay_ms: int
) -> BidOutcome:
    """DELIBERATELY BROKEN. Exists only so scripts/race_demo.py can show the bug.

    Read, check, then write - with no lock and no transaction. Two bids that
    read the same price both pass the check, and whichever write lands last
    wins, even if it is the lower bid. Enabled only with ENABLE_UNSAFE_DEMO.
    """
    auction = await pool.fetchrow(
        f"SELECT {AUCTION_COLUMNS}, now() AS db_now FROM auctions WHERE id = $1", auction_id
    )
    if auction is None:
        raise AuctionNotFound(auction_id)
    reason = _rejection_reason(auction, amount)
    if reason is not None:
        return BidOutcome(False, False, reason, None, auction_dict(auction))

    # The gap between "check" and "write" that every real system has (network,
    # GC pause, a slow query). Widened here so the race shows up every run.
    await asyncio.sleep(delay_ms / 1000)

    bid = await pool.fetchrow(
        f"""
        INSERT INTO bids (auction_id, bidder, amount, request_id, status)
        VALUES ($1, $2, $3, $4, 'accepted')
        RETURNING {BID_COLUMNS}
        """,
        auction_id,
        bidder,
        amount,
        f"unsafe-{uuid.uuid4().hex}",
    )
    updated = await pool.fetchrow(
        f"""
        UPDATE auctions
        SET current_price = $2, leader = $3, bid_count = bid_count + 1, version = version + 1
        WHERE id = $1
        RETURNING {AUCTION_COLUMNS}
        """,
        auction_id,
        amount,
        bidder,
    )
    bid_out, auction_out = bid_dict(bid), auction_dict(updated)
    async with pool.acquire() as conn:
        await _notify(conn, {"type": "bid_accepted", "auction": auction_out, "bid": bid_out})
    return BidOutcome(True, False, None, bid_dict(bid, private=True), auction_out)


async def close_expired(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    """Close every open auction whose end time has passed, exactly once.

    Every API instance runs this on a timer. The UPDATE's WHERE status = 'open'
    is re-checked under the row lock, so when several instances race, only one
    of them flips a given auction and announces it.
    """
    try:
        async with pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                f"""
                UPDATE auctions
                SET status = 'closed', closed_at = now(), version = version + 1
                WHERE status = 'open' AND ends_at <= now()
                RETURNING {AUCTION_COLUMNS}
                """
            )
            closed = [auction_dict(r) for r in rows]
            for auction in closed:
                await _notify(conn, {"type": "auction_closed", "auction": auction})
            return closed
    except asyncpg.exceptions.LockNotAvailableError:
        return []  # a bid holds the row; the next tick will get it


async def close_now(pool: asyncpg.Pool, auction_id: int) -> dict[str, Any] | None:
    """Admin: end an open lot immediately. The current leader wins.

    The UPDATE takes the row lock like a bid does, so a bid in flight either
    commits before the close or is rejected after it - never half of each.
    Returns None if the lot doesn't exist or isn't open.
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            f"""
            UPDATE auctions
            SET status = 'closed', closed_at = now(), ends_at = LEAST(ends_at, now()),
                version = version + 1
            WHERE id = $1 AND status = 'open' AND removed_at IS NULL
            RETURNING {AUCTION_COLUMNS}
            """,
            auction_id,
        )
        if row is None:
            return None
        auction = auction_dict(row)
        await _notify(conn, {"type": "auction_closed", "auction": auction})
        return auction


async def remove(pool: asyncpg.Pool, auction_id: int) -> dict[str, Any] | None:
    """Admin: withdraw a lot. It closes (if open), disappears from public view,
    and every connected viewer is told. Its bids stay for the audit trail."""
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            f"""
            UPDATE auctions
            SET removed_at = now(), status = 'closed',
                closed_at = COALESCE(closed_at, now()), version = version + 1
            WHERE id = $1 AND removed_at IS NULL
            RETURNING {AUCTION_COLUMNS}
            """,
            auction_id,
        )
        if row is None:
            return None
        auction = auction_dict(row)
        await _notify(conn, {"type": "auction_removed", "auction": auction})
        return auction


async def closer_loop(pool: asyncpg.Pool, interval: float) -> None:
    while True:
        try:
            await close_expired(pool)
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive through DB blips
            log.exception("closer tick failed")
        await asyncio.sleep(interval)
