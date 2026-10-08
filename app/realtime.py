"""WebSocket endpoint: /ws/auctions/{id}?bidder=<name>

Server -> client
    snapshot        full current state, read from Postgres (sent on connect,
                    on request, and after the server's event feed reconnects)
    bid_accepted    someone's bid became the new high bid      (broadcast)
    auction_closed  the auction ended; `leader` is the winner  (broadcast)
    bid_result      the outcome of *your* bid                  (to you only)
    pong / error

Client -> server
    {"type": "bid", "amount": 1250, "request_id": "<unique per bid>"}
    {"type": "resync"}   ask for a fresh snapshot
    {"type": "ping"}

Every snapshot and broadcast carries auction.version. A client keeps the
highest version it has seen and ignores anything older, so a snapshot and the
live feed can never be applied out of order.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import Any

import asyncpg
from fastapi import APIRouter, WebSocket

from app import auctions, bidding
from app.config import MAX_AMOUNT
from app.hub import Client

router = APIRouter()

NOT_FOUND = 4404


def _bid_error(request_id: Any, reason: str) -> str:
    return json.dumps(
        {"type": "bid_result", "request_id": request_id, "accepted": False,
         "duplicate": False, "reason": reason, "bid": None, "auction": None}
    )


async def _handle_bid(pool: asyncpg.Pool, client: Client, msg: dict[str, Any]) -> None:
    request_id, amount = msg.get("request_id"), msg.get("amount")
    if client.bidder is None:
        client.offer(_bid_error(request_id, "watch_only"))
        return
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
        client.offer(_bid_error(request_id, "invalid_request_id"))
        return
    if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= MAX_AMOUNT:
        client.offer(_bid_error(request_id, "invalid_amount"))
        return
    try:
        # Shielded: if the socket drops mid-bid, the bid still runs to COMMIT or
        # ROLLBACK rather than being torn down halfway. The client learns the
        # outcome by resending the same request_id after it reconnects.
        outcome = await asyncio.shield(
            bidding.place_bid(pool, client.auction_id, client.bidder, amount, request_id)
        )
    except bidding.AuctionBusy:
        client.offer(_bid_error(request_id, "busy_retry"))
        return
    client.offer(json.dumps({"type": "bid_result", "request_id": request_id, **outcome.as_dict()}))


async def _on_message(pool: asyncpg.Pool, client: Client, text: str) -> None:
    try:
        msg = json.loads(text)
    except ValueError:
        msg = None
    kind = msg.get("type") if isinstance(msg, dict) else None

    if kind == "bid":
        await _handle_bid(pool, client, msg)
    elif kind == "resync":
        snapshot = await auctions.snapshot_message(pool, client.auction_id)
        if snapshot is not None:
            client.offer(snapshot)
    elif kind == "ping":
        client.offer('{"type": "pong"}')
    else:
        client.offer(json.dumps({"type": "error", "code": "bad_message",
                                 "message": 'expected {"type": "bid" | "resync" | "ping"}'}))


@router.websocket("/ws/auctions/{auction_id}")
async def auction_socket(ws: WebSocket, auction_id: int, bidder: str | None = None) -> None:
    state = ws.app.state
    bidder = (bidder or "").strip()[:40] or None  # no name = watch only
    await ws.accept()

    client = Client(ws, auction_id, bidder, state.hub.queue_max)
    # Subscribe first, then read the snapshot. Any event committed in between is
    # queued behind the snapshot and either already reflected in it (same or
    # lower version, ignored by the client) or newer (applied). Nothing is lost.
    state.hub.join(client)
    try:
        snapshot = await auctions.snapshot_message(state.pool, auction_id)
        if snapshot is None:
            await ws.close(code=NOT_FOUND, reason="auction not found")
            return
        client.offer(snapshot)
        await client.serve(partial(_on_message, state.pool))
    finally:
        state.hub.leave(client)
