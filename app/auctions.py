"""Reading and creating auctions, and turning rows into JSON-ready dicts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg

AUCTION_COLUMNS = """
    id, title, description, starting_price, min_increment, current_price,
    leader, bid_count, version, status, ends_at, created_at, closed_at, removed_at, extensions
"""

BID_COLUMNS = "id, auction_id, bidder, amount, request_id, status, reason, created_at"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def min_next_bid(row: asyncpg.Record | dict[str, Any]) -> int:
    """The lowest amount the next bid may be."""
    if row["current_price"] is None:
        return row["starting_price"]
    return row["current_price"] + row["min_increment"]


def auction_dict(row: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "starting_price": row["starting_price"],
        "min_increment": row["min_increment"],
        "current_price": row["current_price"],
        "leader": row["leader"],
        "bid_count": row["bid_count"],
        "min_next_bid": min_next_bid(row),
        "status": row["status"],
        "ends_at": _iso(row["ends_at"]),
        "created_at": _iso(row["created_at"]),
        "closed_at": _iso(row["closed_at"]),
        "removed_at": _iso(row["removed_at"]),
        "extensions": row["extensions"],
        "version": row["version"],
    }


def bid_dict(row: asyncpg.Record, *, private: bool = False) -> dict[str, Any]:
    """A bid as JSON. `request_id` is the bidder's retry key, so it only goes
    back to the bidder who sent it (and to the admin), never to everyone."""
    bid = {
        "id": row["id"],
        "auction_id": row["auction_id"],
        "bidder": row["bidder"],
        "amount": row["amount"],
        "status": row["status"],
        "reason": row["reason"],
        "created_at": _iso(row["created_at"]),
    }
    if private:
        bid["request_id"] = row["request_id"]
    return bid


async def create(
    pool: asyncpg.Pool,
    *,
    title: str,
    description: str,
    starting_price: int,
    min_increment: int,
    duration_seconds: int,
) -> dict[str, Any]:
    row = await pool.fetchrow(
        f"""
        INSERT INTO auctions (title, description, starting_price, min_increment, ends_at)
        VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5::double precision))
        RETURNING {AUCTION_COLUMNS}
        """,
        title,
        description,
        starting_price,
        min_increment,
        float(duration_seconds),
    )
    return auction_dict(row)


async def list_all(
    pool: asyncpg.Pool, limit: int = 100, *, include_removed: bool = False
) -> list[dict[str, Any]]:
    rows = await pool.fetch(
        f"""
        SELECT {AUCTION_COLUMNS} FROM auctions
        WHERE $2 OR removed_at IS NULL
        ORDER BY status = 'open' DESC,
                 CASE WHEN status = 'open' THEN ends_at END ASC,
                 ends_at DESC
        LIMIT $1
        """,
        limit,
        include_removed,
    )
    return [auction_dict(r) for r in rows]


async def snapshot(
    pool: asyncpg.Pool, auction_id: int, bids_limit: int = 25
) -> dict[str, Any] | None:
    """Current auction state plus its latest accepted bids, read from Postgres.

    Both reads run in one REPEATABLE READ transaction, so the bid list always
    matches the auction's version - a bid can't land between the two queries.
    """
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        row = await conn.fetchrow(
            f"SELECT {AUCTION_COLUMNS}, now() AS db_now FROM auctions"
            " WHERE id = $1 AND removed_at IS NULL",
            auction_id,
        )
        if row is None:
            return None
        bids = await conn.fetch(
            f"""
            SELECT {BID_COLUMNS} FROM bids
            WHERE auction_id = $1 AND status = 'accepted'
            ORDER BY id DESC LIMIT $2
            """,
            auction_id,
            bids_limit,
        )
    return {
        "auction": auction_dict(row),
        "bids": [bid_dict(b) for b in bids],
        "server_time": _iso(row["db_now"]),
    }


async def snapshot_message(
    pool: asyncpg.Pool, auction_id: int, *, instance: str | None = None
) -> str | None:
    snap = await snapshot(pool, auction_id)
    if snap is None:
        return None
    message = {"type": "snapshot", **snap}
    if instance:
        message["instance"] = instance  # which API instance this socket is on
    return json.dumps(message)


async def bid_history(
    pool: asyncpg.Pool, auction_id: int, *, limit: int, include_rejected: bool
) -> list[dict[str, Any]] | None:
    if await pool.fetchval(
        "SELECT 1 FROM auctions WHERE id = $1 AND removed_at IS NULL", auction_id
    ) is None:
        return None
    rows = await pool.fetch(
        f"""
        SELECT {BID_COLUMNS} FROM bids
        WHERE auction_id = $1 AND ($2 OR status = 'accepted')
        ORDER BY id DESC LIMIT $3
        """,
        auction_id,
        include_rejected,
        limit,
    )
    return [bid_dict(r) for r in rows]
