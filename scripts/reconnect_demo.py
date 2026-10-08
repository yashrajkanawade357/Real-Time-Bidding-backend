"""A narrated walk through disconnects, stale bids and retries.

    python scripts/reconnect_demo.py [--base-url http://127.0.0.1:8000]

Alice watches an auction over a WebSocket, loses her connection, bids from a
stale view while offline, reconnects, and retries a bid. Every step prints
what the server said and why.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
from websockets.asyncio.client import connect


def say(step: str, text: str) -> None:
    print(f"\n{step}  {text}")


def show(label: str, auction: dict) -> None:
    print(f"      {label}: price={auction['current_price']} leader={auction['leader']} "
          f"version={auction['version']} next_min={auction['min_next_bid']}")


async def recv(ws, *kinds: str) -> dict:
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        if msg["type"] in kinds:
            return msg


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    base = parser.parse_args().base_url.rstrip("/")
    ws_base = base.replace("http", "ws", 1)

    async with httpx.AsyncClient(base_url=base, timeout=10) as http:
        try:
            await http.get("/health")
        except httpx.HTTPError:
            print(f"No server at {base}. Start it with: uvicorn app.main:app")
            return 2

        async def rest_bid(bidder: str, amount: int) -> None:
            r = await http.post(f"/auctions/{aid}/bids", json={"bidder": bidder, "amount": amount})
            print(f"      {bidder} bids {amount} over REST -> {r.status_code} "
                  f"{'accepted' if r.status_code == 201 else r.json()['reason']}")

        auction = (await http.post("/auctions", json={
            "title": "Vintage camera (reconnect demo)", "starting_price": 100,
            "min_increment": 10, "duration_seconds": 600,
        })).json()
        aid = auction["id"]
        say("1.", f"Auction #{aid} created: starts at 100, bids must rise by 10.")

        alice = await connect(f"{ws_base}/ws/auctions/{aid}?bidder=alice")
        snap = await recv(alice, "snapshot")
        say("2.", "Alice connects. The first thing she receives is a snapshot read from Postgres.")
        show("snapshot", snap["auction"])

        say("3.", "Bob bids. Alice sees it live, pushed over her socket.")
        await rest_bid("bob", 100)
        show("live event", (await recv(alice, "bid_accepted"))["auction"])
        seen_price = 100

        say("4.", "Alice's connection drops (wifi blip). Nothing about the auction changes on the server.")
        await alice.close()

        say("5.", "While she's offline, the auction moves on:")
        await rest_bid("carol", 150)
        await rest_bid("bob", 200)

        stale = seen_price + 60
        say("6.", f"Alice, still seeing price {seen_price}, queues a bid of {stale} in her app's outbox.")

        alice = await connect(f"{ws_base}/ws/auctions/{aid}?bidder=alice")
        snap = await recv(alice, "snapshot")
        say("7.", "Alice reconnects. Her snapshot comes from the database, not anyone's memory:")
        show("snapshot", snap["auction"])

        say("8.", "Her app flushes the outbox. The server judges the bid against the *current* price:")
        await alice.send(json.dumps({"type": "bid", "amount": stale, "request_id": "alice-outbox-1"}))
        result = await recv(alice, "bid_result")
        print(f"      bid {stale} -> accepted={result['accepted']} reason={result['reason']} "
              f"(minimum is now {result['auction']['min_next_bid']})")

        say("9.", "She bids 250. Her network stalls before the reply, so the app resends it with the SAME request_id:")
        for attempt in (1, 2):
            await alice.send(json.dumps({"type": "bid", "amount": 250, "request_id": "alice-250"}))
            result = await recv(alice, "bid_result")
            print(f"      attempt {attempt}: accepted={result['accepted']} duplicate={result['duplicate']} "
                  f"bid_id={result['bid']['id']} bid_count={result['auction']['bid_count']}")
        await alice.close()

        final = (await http.get(f"/auctions/{aid}")).json()
        say("10.", "Final state, read back over REST:")
        show("auction", final["auction"])
        print("      accepted bids, oldest first: "
              + ", ".join(f"{b['bidder']} {b['amount']}" for b in reversed(final["bids"])))
        ok = (final["auction"]["current_price"], final["auction"]["leader"], final["auction"]["bid_count"]) == (250, "alice", 4)
        print(f"\n{'[ ok ]' if ok else '[FAIL]'} 4 accepted bids, the stale one rejected, the retry counted once.")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
