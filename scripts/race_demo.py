"""Fire hundreds of near-simultaneous bids at one auction, then audit the result.

    python scripts/race_demo.py                  # the real, locked bid path
    python scripts/race_demo.py --unsafe         # also the broken lock-free path, side by side
    python scripts/race_demo.py --bids 500 --base-url http://localhost:8000

--unsafe needs the server started with ENABLE_UNSAFE_DEMO=true.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import secrets
import ssl
import sys
import time
from urllib.parse import urlsplit

import httpx

OK, BAD = "[ ok ]", "[FAIL]"


async def make_accounts(base: str, names: list[str]) -> dict[str, tuple[str, httpx.AsyncClient]]:
    """On servers that require login: one throwaway account per name. Returns
    name -> (display name, a client carrying that account's session cookie)."""
    suffix = secrets.token_hex(2)
    accounts = {}
    for name in names:
        client = httpx.AsyncClient(base_url=base, timeout=15)
        display = f"{name}-{suffix}"
        r = await client.post("/auth/signup", json={
            "email": f"{display}@example.test", "password": secrets.token_urlsafe(16), "display_name": display})
        r.raise_for_status()
        accounts[name] = (display, client)
    return accounts


async def open_lot(http: httpx.AsyncClient, admin_key: str | None, **lot) -> dict:
    """Open a lot: through the admin API when a key is given (servers where
    visitors can't open lots), otherwise through the public endpoint."""
    if admin_key:
        r = await http.post("/admin/api/lots", json=lot, headers={"Authorization": f"Bearer {admin_key}"})
    else:
        r = await http.post("/auctions", json=lot)
    if r.status_code == 403:
        raise SystemExit("This server doesn't let visitors open lots. Pass --admin-key (or set BIDDING_ADMIN_KEY).")
    r.raise_for_status()
    return r.json()


class Bidder:
    """One bidder = one TCP connection, opened ahead of time so that every
    request can be written at the same instant. (A pooled HTTP client would
    queue them, which spreads the 'simultaneous' bids out and hides races.)"""

    def __init__(self, base_url: str, cookie: str | None = None) -> None:
        self.cookie = cookie
        parts = urlsplit(base_url)
        self.host = parts.hostname or "127.0.0.1"
        self.tls = parts.scheme == "https"
        self.port = parts.port or (443 if self.tls else 80)

    async def open(self) -> None:
        ctx = ssl.create_default_context() if self.tls else None
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port, ssl=ctx)

    async def post(self, path: str, payload: dict) -> int:
        body = json.dumps(payload).encode()
        head = (f"POST {path} HTTP/1.1\r\nHost: {self.host}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n")
        if self.cookie:
            head += f"Cookie: bf_session={self.cookie}\r\n"
        self.writer.write((head + "\r\n").encode() + body)
        await self.writer.drain()
        raw = await self.reader.read()
        self.writer.close()
        return int(raw.split(b" ", 2)[1])  # "HTTP/1.1 201 Created" -> 201


async def run_round(http: httpx.AsyncClient, *, n: int, unsafe: bool, seed: int, admin_key: str | None) -> bool:
    label = "UNSAFE  read -> check -> write, no lock" if unsafe else "SAFE    row lock + one transaction per bid"
    auction = await open_lot(http, admin_key, title=f"Race demo ({'unsafe' if unsafe else 'safe'})",
                             starting_price=100, min_increment=1, duration_seconds=600)
    aid = auction["id"]

    amounts = list(range(100, 100 + n))
    random.Random(seed).shuffle(amounts)
    path = f"/auctions/{aid}/bids" + ("/unsafe" if unsafe else "")

    # Servers that require login name the bidder from the session: share the
    # bids among a few throwaway accounts.
    login = (await http.get("/config")).json().get("require_login", False) and not unsafe
    if login:
        accounts = list((await make_accounts(str(http.base_url), [f"racer{i}" for i in range(5)])).values())
        cookies = {amount: accounts[i % 5][1].cookies.get("bf_session") for i, amount in enumerate(amounts)}
        bidders = {amount: accounts[i % 5][0] for i, amount in enumerate(amounts)}
        for _, client in accounts:
            await client.aclose()
    else:
        cookies = {amount: None for amount in amounts}
        bidders = {amount: f"bidder-{i % 37:02d}" for i, amount in enumerate(amounts)}

    conns = [Bidder(str(http.base_url), cookies[a]) for a in amounts]
    await asyncio.gather(*(c.open() for c in conns))  # all connected, nothing sent yet
    go = asyncio.Event()

    async def fire(conn: Bidder, amount: int) -> int:
        await go.wait()
        return await conn.post(path, {"bidder": bidders[amount], "amount": amount,
                                      "request_id": f"race-{aid}-{amount}"})

    tasks = [asyncio.create_task(fire(c, a)) for c, a in zip(conns, amounts)]
    await asyncio.sleep(0.1)
    started = time.perf_counter()
    go.set()  # release every bid at once
    statuses = await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - started

    if 404 in statuses:
        print(f"\n{label}\n  server refused the unsafe endpoint - restart it with ENABLE_UNSAFE_DEMO=true")
        return True
    told_accepted = statuses.count(201)
    errors = [s for s in statuses if s not in (201, 409)]

    final = (await http.get(f"/auctions/{aid}")).json()["auction"]
    accepted = (await http.get(f"/auctions/{aid}/bids", params={"limit": 500})).json()
    accepted.reverse()  # oldest first = the order the bids committed
    seq = [b["amount"] for b in accepted]
    drops = sum(1 for prev, nxt in zip(seq, seq[1:]) if nxt <= prev)
    top = max(amounts)

    checks = [
        (final["current_price"] == top,
         f"final price {final['current_price']} is the highest bid sent ({top})"),
        (final["leader"] == bidders[top],
         f"winner is {final['leader']}; the highest bid came from {bidders[top]}"),
        (drops == 0,
         f"accepted bids only ever went up ({drops} times a lower bid replaced a higher one)"),
        (final["bid_count"] == len(seq) == told_accepted,
         f"{told_accepted} bidders were told 'accepted'; the auction counts {final['bid_count']}; "
         f"{len(seq)} accepted rows stored"),
        (not errors, f"no server errors ({len(errors)} non-201/409 responses)"),
    ]

    print(f"\n{label}")
    print(f"  auction #{aid}: {n} bids released at once, amounts {min(amounts)}..{top} shuffled")
    print(f"  {told_accepted} accepted, {n - told_accepted} rejected, in {elapsed:.2f}s "
          f"({n / elapsed:,.0f} bids/s)")
    for passed, text in checks:
        print(f"  {OK if passed else BAD} {text}")
    return all(p for p, _ in checks)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--bids", type=int, default=300)
    parser.add_argument("--unsafe", action="store_true", help="also run the lock-free path")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--admin-key", default=os.environ.get("BIDDING_ADMIN_KEY"),
                        help="open the lots through the admin API (default: $BIDDING_ADMIN_KEY)")
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as http:
        try:
            await http.get("/health")
        except httpx.HTTPError:
            print(f"No server at {args.base_url}. Start it with: uvicorn app.main:app")
            return 2
        safe_ok = await run_round(http, n=args.bids, unsafe=False, seed=args.seed, admin_key=args.admin_key)
        if args.unsafe:
            await run_round(http, n=args.bids, unsafe=True, seed=args.seed, admin_key=args.admin_key)
            print(f"\nSame {args.bids} bids, same shuffle. The only difference is the lock.")
    return 0 if safe_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
