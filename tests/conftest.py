"""Tests run against a real Postgres - locking behaviour can't be faked.

Set TEST_DATABASE_URL (or put it in .env). The database is truncated before
every test, so never point it at data you care about.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import asyncpg
import pytest
import uvicorn

from app import db
from app.config import load_settings
from app.main import create_app

TEST_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/bidding_test"
)


@pytest.fixture
def settings():
    # Pin everything a developer's .env might change, so tests behave the same everywhere.
    return replace(
        load_settings(),
        database_url=TEST_DSN,
        pool_max=30,
        closer_interval=0.2,
        listener_healthcheck=1.0,
        enable_unsafe_demo=False,
        demo_restock=False,
        admin_key=None,
        judge_access=False,
        require_login=False,
        cookie_secure=False,
        soft_close_seconds=30,
        public_lot_creation=True,
        trusted_proxy_hops=0,
        rate_limits=True,
        cors_origins=(),
        instance_name="test-instance",
    )


@pytest.fixture
async def pool(settings) -> AsyncIterator[asyncpg.Pool]:
    try:
        pool = await db.create_pool(settings)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"test database unavailable ({exc}); set TEST_DATABASE_URL")
    await db.migrate(pool)
    await pool.execute(
        "TRUNCATE bids, auctions, admin_actions, access_keys, instances, sessions, users"
        " RESTART IDENTITY CASCADE"
    )
    yield pool
    await pool.close()


@contextlib.asynccontextmanager
async def running_server(settings) -> AsyncIterator[str]:
    """A real uvicorn server on a free port. Yields "127.0.0.1:<port>"."""
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=0, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()  # surface the startup error
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def wait_for_listener(addr: str) -> None:
    """Block until the instance's LISTEN connection is up, so no event is missed."""
    import httpx

    async with httpx.AsyncClient(base_url=f"http://{addr}") as http:
        for _ in range(100):
            if (await http.get("/health")).json()["event_listener"]:
                return
            await asyncio.sleep(0.05)
    raise AssertionError("event listener never connected")


async def recv_until(ws, kind: str, timeout: float = 5.0, **match: Any) -> dict[str, Any]:
    """Read messages until one of type `kind` (optionally matching fields) arrives."""
    async def _loop():
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == kind and all(msg.get(k) == v for k, v in match.items()):
                return msg

    return await asyncio.wait_for(_loop(), timeout)


async def recv_all(ws, *kinds: str, timeout: float = 5.0) -> dict[str, dict[str, Any]]:
    """Read until one message of each type in `kinds` has arrived, in any order
    (a bidder's own bid_result and the broadcast race each other)."""
    found: dict[str, dict[str, Any]] = {}

    async def _loop():
        while len(found) < len(kinds):
            msg = json.loads(await ws.recv())
            if msg["type"] in kinds:
                found.setdefault(msg["type"], msg)
        return found

    return await asyncio.wait_for(_loop(), timeout)


async def assert_consistent(pool: asyncpg.Pool, auction_id: int) -> None:
    """The invariants that must hold no matter what happened concurrently."""
    auction = await pool.fetchrow("SELECT * FROM auctions WHERE id = $1", auction_id)
    accepted = await pool.fetch(
        "SELECT bidder, amount FROM bids WHERE auction_id = $1 AND status = 'accepted' ORDER BY id",
        auction_id,
    )
    assert auction["bid_count"] == len(accepted)
    if not accepted:
        assert auction["current_price"] is None and auction["leader"] is None
        return
    # Accepted bids, in the order they committed, strictly climb by >= increment.
    amounts = [b["amount"] for b in accepted]
    assert amounts[0] >= auction["starting_price"]
    for prev, nxt in zip(amounts, amounts[1:]):
        assert nxt >= prev + auction["min_increment"], f"{nxt} accepted after {prev}"
    # The price on the auction is the last (= highest) accepted bid, by its bidder.
    assert auction["current_price"] == amounts[-1] == max(amounts)
    assert auction["leader"] == accepted[-1]["bidder"]
