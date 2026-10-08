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

from app import admin, auctions, bidding
from app.config import MAX_AMOUNT
from app.hub import Client
from app.security import RateLimiter, client_ip

router = APIRouter()

NOT_FOUND = 4404
UNAUTHORIZED = 4401
TRY_AGAIN_LATER = 1013

# Per socket: a person clicking "Bid" never gets near this; a script hammering
# the socket gets "rate_limited" instead of a database transaction per message.
SOCKET_BIDS_PER_SEC = 10
SOCKET_BID_BURST = 20


def _bid_error(request_id: Any, reason: str) -> str:
    return json.dumps(
        {"type": "bid_result", "request_id": request_id, "accepted": False,
         "duplicate": False, "reason": reason, "bid": None, "auction": None}
    )


def _ip_of(ws: WebSocket) -> str:
    peer = ws.scope["client"][0] if ws.scope.get("client") else None
    return client_ip(ws.scope.get("headers", []), peer, ws.app.state.settings.trusted_proxy_hops)


async def _handle_bid(pool: asyncpg.Pool, limiter: RateLimiter, client: Client, msg: dict[str, Any]) -> None:
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
    if not limiter.allow(str(id(client))):
        client.offer(_bid_error(request_id, "rate_limited"))
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


async def _on_message(
    pool: asyncpg.Pool, limiter: RateLimiter, instance: str, client: Client, text: str
) -> None:
    try:
        msg = json.loads(text)
    except ValueError:
        msg = None
    kind = msg.get("type") if isinstance(msg, dict) else None

    if kind == "bid":
        await _handle_bid(pool, limiter, client, msg)
    elif kind == "resync":
        snapshot = await auctions.snapshot_message(pool, client.auction_id, instance=instance)
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
    settings = state.settings
    bidder = (bidder or "").strip()[:40] or None  # no name = watch only
    ip = _ip_of(ws)
    await ws.accept()
    if settings.rate_limits and state.hub.connections_from(ip) >= settings.ws_connections_per_ip:
        await ws.close(code=TRY_AGAIN_LATER, reason="too many connections from your address")
        return

    client = Client(ws, auction_id, bidder, state.hub.queue_max, ip=ip)
    # Subscribe first, then read the snapshot. Any event committed in between is
    # queued behind the snapshot and either already reflected in it (same or
    # lower version, ignored by the client) or newer (applied). Nothing is lost.
    state.hub.join(client)
    try:
        snapshot = await auctions.snapshot_message(state.pool, auction_id, instance=settings.instance_name)
        if snapshot is None:
            await ws.close(code=NOT_FOUND, reason="auction not found")
            return
        client.offer(snapshot)
        await client.serve(partial(_on_message, state.pool, state.socket_bid_limiter, settings.instance_name))
    finally:
        state.hub.leave(client)


async def _admin_message(client: Client, text: str) -> None:
    if '"ping"' in text:
        client.offer('{"type": "pong"}')


@router.websocket("/ws/admin")
async def admin_socket(ws: WebSocket) -> None:
    """Every event for every lot, for the admin portal's live feed.

    The key arrives in the first message, not the URL, so it never lands in
    proxy or CDN access logs.
    """
    state = ws.app.state
    settings = state.settings
    ip = _ip_of(ws)
    await ws.accept()
    if not admin.admin_enabled(settings):
        await ws.close(code=NOT_FOUND, reason="admin is disabled")
        return
    if not state.admin_failures.peek(ip):
        await ws.close(code=TRY_AGAIN_LATER, reason="too many failed attempts")
        return
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5))
        supplied = first.get("key") if isinstance(first, dict) and first.get("type") == "auth" else None
    except (TimeoutError, ValueError, KeyError, RuntimeError):
        supplied = None
    except Exception:  # client went away
        return
    role = await admin.role_for(state, supplied)
    if role is None:
        state.admin_failures.allow(ip)
        await ws.close(code=UNAUTHORIZED, reason="bad key")
        return

    client = Client(ws, 0, None, state.hub.queue_max, ip=ip)
    state.hub.join_admin(client)
    try:
        client.offer(json.dumps({"type": "hello", "instance": settings.instance_name, "role": role}))
        await client.serve(_admin_message)
    finally:
        state.hub.leave(client)
