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
import random
import ssl
import sys
import time
from urllib.parse import urlsplit

import httpx

OK, BAD = "[ ok ]", "[FAIL]"


class Bidder:
    """One bidder = one TCP connection, opened ahead of time so that every
    request can be written at the same instant. (A pooled HTTP client would
    queue them, which spreads the 'simultaneous' bids out and hides races.)"""

    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        self.host = parts.hostname or "127.0.0.1"
        self.tls = parts.scheme == "https"
        self.port = parts.port or (443 if self.tls else 80)

    async def open(self) -> None:
        ctx = ssl.create_default_context() if self.tls else None
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port, ssl=ctx)

    async def post(self, path: str, payload: dict) -> int:
        body = json.dumps(payload).encode()
        self.writer.write(
            f"POST {path} HTTP/1.1\r\nHost: {self.host}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )
        await self.writer.drain()
        raw = await self.reader.read()
        self.writer.close()
        return int(raw.split(b" ", 2)[1])  # "HTTP/1.1 201 Created" -> 201


async def run_round(http: httpx.AsyncClient, *, n: int, unsafe: bool, seed: int) -> bool:
    label = "UNSAFE  read -> check -> write, no lock" if unsafe else "SAFE    row lock + one transaction per bid"
    auction = (await http.post("/auctions", json={
        "title": f"Race demo ({'unsafe' if unsafe else 'safe'})",
        "starting_price": 100, "min_increment": 1, "duration_seconds": 600,
    })).json()
    aid = auction["id"]

    amounts = list(range(100, 100 + n))
    random.Random(seed).shuffle(amounts)
    bidders = {amount: f"bidder-{i % 37:02d}" for i, amount in enumerate(amounts)}
    path = f"/auctions/{aid}/bids" + ("/unsafe" if unsafe else "")

    conns = [Bidder(str(http.base_url)) for _ in amounts]
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
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as http:
        try:
            await http.get("/health")
        except httpx.HTTPError:
            print(f"No server at {args.base_url}. Start it with: uvicorn app.main:app")
            return 2
        safe_ok = await run_round(http, n=args.bids, unsafe=False, seed=args.seed)
        if args.unsafe:
            await run_round(http, n=args.bids, unsafe=True, seed=args.seed)
            print(f"\nSame {args.bids} bids, same shuffle. The only difference is the lock.")
    return 0 if safe_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
