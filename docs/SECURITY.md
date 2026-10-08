# Security

How this service is protected, how each control is checked, and what is
deliberately left out of scope.

## What is being protected

| Asset | Why it matters |
|---|---|
| **Correct outcomes** | The highest valid bid must win; no bid may be lost, doubled or applied out of order. |
| **The live service** | A public link has to stay up when someone floods it. |
| **Admin control** | Only the auctioneer may open, close or remove lots. |
| **The database** | It is the only copy of state, so it must not be reachable from the internet. |

**Who it is protected from:** anonymous visitors, scripts flooding the API,
anyone trying to impersonate the admin, and anyone trying to reach the server
around HTTPS.

## The path a request takes

```
Internet ──HTTPS──▶ CloudFront ──HTTP, port 80──▶ EC2 security group (CloudFront addresses only)
                                                  └─▶ nginx ──▶ API instance 1 / 2 ──▶ PostgreSQL
                                                         (Docker network only; nothing else published)
```

## Controls

### Correctness under concurrency

- **Bids are decided in the database, under a row lock.** Every bid is one
  transaction that locks the lot's row and checks the rules against the
  committed price, so arrival order cannot change the winner.
  *Checked by:* `test_concurrent_bids_highest_always_wins` (300 at once, three
  shuffles), `test_equal_simultaneous_bids_only_one_wins`, and the race demo,
  which CI runs on every push.
- **Retries cannot double-bid.** Each bid carries an idempotency key, and the
  key is checked under the same lock.
  *Checked by:* `test_concurrent_retries_of_one_request_create_one_bid`.
- **Admin actions take the same lock.** "Close now" and "Remove" are
  conditional updates on the locked row. A bid in flight lands before the
  close or is rejected after it, never half of each.

### Input

- **SQL injection.** Every value goes to Postgres as a bound parameter. The
  only string-built SQL inserts fixed column lists, never user input.
- **Validation.** Amounts are integers with upper bounds, titles and names have
  length limits, and `request_id`s are 1–64 characters. Malformed WebSocket
  messages get an error reply and never reach the database.
- **Size limits.** Request bodies are capped at 16 KB, by nginx and again in
  the app. WebSocket messages are capped at 64 KB (`--ws-max-size`).
  *Checked by:* `test_oversized_bodies_are_refused`.

### Abuse and flooding

| Limit | Value | Where |
|---|---|---|
| REST bids per client address | 30/s sustained, bursts of 400 | `POST /auctions/{id}/bids` → `429` |
| Bids per WebSocket | 10/s, bursts of 20 | `bid_result` reason `rate_limited` |
| Open WebSockets per address | 50 per instance | close code `1013` |
| New lots per address (only where visitors may open lots) | 5 per minute | `429` |
| Wrong admin keys per address | 10, then 1 every 6 s | `429`, also blocks the right key |

- **The client address can't be spoofed.** The client's address is read from
  `X-Forwarded-For`, counting **N entries from the right**, where N is the
  number of trusted proxies (CloudFront and nginx = 2). Entries further left
  are client-supplied and ignored.
- **Nobody can go around the proxies.** The origin only accepts connections
  from CloudFront, so the whole chain is trusted.

*Checked by:* `test_bid_flood_from_one_address_is_throttled`,
`test_socket_bid_spam_is_rate_limited`,
`test_too_many_sockets_from_one_address_are_refused`,
`test_client_is_the_nth_address_from_the_right`.

### The admin portal

- **The key.** It is generated on the server at first boot
  (`openssl rand`, 264 bits) and stored in a root-only `.env` (mode 600). It
  is never in the repository, the image or the EC2 user data. Keys shorter
  than 24 characters are refused at startup.
  *Checked by:* `test_short_keys_are_refused_at_startup`.
- **Sending and checking it.**
  - It is sent as `Authorization: Bearer …` and compared in constant time
    (`hmac.compare_digest`), so response timing leaks nothing.
  - The admin WebSocket receives the key in its **first message**, not the
    URL, so it never appears in nginx or CloudFront logs.
- **Lockout.** Ten wrong keys from one address lock it out, and while locked
  out even the right key is refused.
  *Checked by:* `test_repeated_wrong_keys_lock_the_address_out`.
- **Off unless configured.** With no key configured, `/admin/api/*` answers
  `404` and the admin socket closes `4404`, as if they didn't exist.
  *Checked by:* `test_admin_is_invisible_without_a_configured_key`.
- **In the browser.** The key is kept in `sessionStorage`: forgotten when the
  tab closes, never sent to any other site.
- **Visitors can't open lots on the public server** (`PUBLIC_LOT_CREATION=false`),
  so nobody can fill the demo with junk.
  *Checked by:* `test_public_lot_creation_can_be_switched_off`, and CI.

### The judge key

The public demo also has a second, **shareable** key (`JUDGE_ACCESS=true`), so
judges can use the admin portal without asking. The landing page shows it, and
its "Open the admin portal" button unlocks the portal in one click.

- **In the URL, never in logs.** That button passes the key in the URL
  *fragment* (`/admin#key=…`), which browsers never send to the server. The
  portal reads it and wipes it from the address bar at once.
- **Full control over lots, with limits.** A judge can open, close and remove
  lots, but:
  - at most **12 open lots** at a time;
  - **30 changes per minute** per address;
  - it **can't touch keys**.
  *Checked by:* `test_judges_can_only_keep_a_few_lots_open`,
  `test_judge_changes_are_rate_limited`, `test_judges_cannot_touch_keys`.
- **Under the owner's control.** It lives in the database, not the
  environment, so you can **rotate** it or **switch it off** from the portal
  instantly, with no redeploy. A judge's open tab locks itself within seconds
  of a rotation, and the landing page shows the new key.
  *Checked by:* `test_owner_rotates_and_disables_the_judge_key`.
- **Every change is attributed.** Opening, closing and removing lots, and
  key changes, are written to `admin_actions` with the role that made them.
  The portal shows that log; client addresses are visible to the owner only.
  *Checked by:* `test_every_change_is_attributed`.
- **Self-healing.** If someone removes every lot, the demo restock reopens fresh
  ones within about 20 seconds.

### Browser

Every response sets:

| Header | Value |
|---|---|
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'` |
| `X-Frame-Options` | `DENY` |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `no-referrer` |
| `Cross-Origin-Opener-Policy` | `same-origin` |
| `Permissions-Policy` | camera, microphone, geolocation and payment off |

- **No inline scripts or styles.** The pages are written so the strict CSP
  needs no exceptions. The interactive API docs at `/docs` load Swagger UI from
  a CDN, so only that path is exempt from the CSP.
- **XSS.** Names and titles that users type are only ever inserted with
  `textContent`, never `innerHTML`; the CSP is a second layer.
- **No server banner.** uvicorn's `Server` header is turned off, and nginx
  sends no version.
- **No cross-origin API calls.** CORS is off by default, because the pages are
  served from the same address as the API.
- **Retry keys stay private.** A bid's `request_id` goes back only to the
  bidder who sent it (and to the admin), never in broadcasts, snapshots or
  public history.
  *Checked by:* `test_pages_carry_security_headers`,
  `test_request_ids_are_private_to_the_bidder`.

### Network and hosting

- **HTTPS.** CloudFront's certificate serves the site; HTTP redirects to
  HTTPS, and live updates use `wss://`.
- **Only CloudFront reaches the server.** The security group allows port 80
  from the `com.amazonaws.global.cloudfront.origin-facing` prefix list only.
- **Nothing else is exposed.**
  - Postgres and the API containers are not published at all.
  - There is no SSH: port 22 is closed and there is no key pair.
  - Deployment and updates go through user data and reboots.
- **IMDSv2 required** on the instance (`HttpTokens=required`).
- **Database password.** It is generated on the instance and lives only in the
  root-only `.env`.
- **The container runs as a non-root user** (uid 10001) on a slim image.

*Checked by:* CI's `aws-stack` job, which boots the production stack and
asserts that only port 80 is published, the unsafe endpoint is `404`, public
lot creation is `403`, the admin API rejects a wrong key, and the security
headers are present. The live server was also probed from outside: Postgres
(5432) and SSH (22) don't answer.

### Dependencies

`pip-audit` checks every runtime dependency against known-vulnerability
databases in CI on every push. Result at the time of writing: **no known
vulnerabilities**.

## Accepted risks and what production would add

| Gap | Why it's acceptable here | Production fix |
|---|---|---|
| **Bidders aren't authenticated.** Anyone can bid under any name. | It's a demo of concurrency, not identity. | Sign-in (e.g. Amazon Cognito), with the bidder name taken from the verified token, never the client. |
| **One shared admin key**, no per-person accounts. | There is one auctioneer. | SSO or Cognito with roles, and every admin action attributed to a person. |
| **The judge key is public**, so anyone who finds the landing page can open, close and remove lots. | That's the point: judges try it without asking. The damage is bounded (12 open lots, 30 changes/min), every action is logged, lots reopen themselves, and the owner can rotate or switch the key off in one click. | Per-judge invitations with expiry, issued from the owner account. |
| **The admin key can be read from the EC2 console log** (`aws-launch.sh admin-key`). | Only principals in the AWS account can read it. | AWS Secrets Manager or SSM Parameter Store, read through an instance role. |
| **CloudFront → origin is plain HTTP** inside AWS's network. | The origin only accepts CloudFront. | TLS to the origin, or CloudFront VPC origins with no public IP at all. |
| **Rate limits are per instance and in memory**, so two instances double the effective limit. | Enough to stop casual floods. | AWS WAF rate-based rules on CloudFront, or a shared limiter. |
| **Postgres runs in a container on the same instance**, without backups. | The demo data is disposable. | Amazon RDS with automated backups and Multi-AZ. |

## Reporting a problem

Please contact the repository owner privately rather than opening a public
issue.
