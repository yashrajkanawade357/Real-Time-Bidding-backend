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
import os
import secrets
import sys

import httpx
from websockets.asyncio.client import connect


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
    parser.add_argument("--admin-key", default=os.environ.get("BIDDING_ADMIN_KEY"),
                        help="open the lot through the admin API (default: $BIDDING_ADMIN_KEY)")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    ws_base = base.replace("http", "ws", 1)

    async with httpx.AsyncClient(base_url=base, timeout=10) as http:
        try:
            await http.get("/health")
        except httpx.HTTPError:
            print(f"No server at {base}. Start it with: uvicorn app.main:app")
            return 2

        # Servers that require login take the bidder from the session, so each
        # person in the story gets a throwaway account.
        login = (await http.get("/config")).json().get("require_login", False)
        accounts = await make_accounts(base, ["alice", "bob", "carol"]) if login else {}

        def name(person: str) -> str:
            return accounts[person][0] if login else person

        def alice_socket():
            if not login:
                return connect(f"{ws_base}/ws/auctions/{aid}?bidder=alice")
            token = accounts["alice"][1].cookies.get("bf_session")
            return connect(f"{ws_base}/ws/auctions/{aid}", additional_headers={"Cookie": f"bf_session={token}"})

        async def rest_bid(person: str, amount: int) -> None:
            if login:
                r = await accounts[person][1].post(f"/auctions/{aid}/bids", json={"amount": amount})
            else:
                r = await http.post(f"/auctions/{aid}/bids", json={"bidder": person, "amount": amount})
            print(f"      {name(person)} bids {amount} over REST -> {r.status_code} "
                  f"{'accepted' if r.status_code == 201 else r.json()['reason']}")

        auction = await open_lot(http, args.admin_key, title="Vintage camera (reconnect demo)",
                                 starting_price=100, min_increment=10, duration_seconds=600)
        aid = auction["id"]
        say("1.", f"Auction #{aid} created: starts at 100, bids must rise by 10.")

        alice = await alice_socket()
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

        alice = await alice_socket()
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
        ok = (final["auction"]["current_price"], final["auction"]["leader"], final["auction"]["bid_count"]) == (250, name("alice"), 4)
        for _, client in accounts.values():
            await client.aclose()
        print(f"\n{'[ ok ]' if ok else '[FAIL]'} 4 accepted bids, the stale one rejected, the retry counted once.")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
