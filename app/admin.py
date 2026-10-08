"""Admin API behind an access key: /admin/api/*

Two keys, two roles:

* **owner**: ADMIN_KEY from the server's environment. Full control, including
  the judge key itself.
* **judge**: a second key kept in the database, meant to be shared (the
  landing page shows it when JUDGE_ACCESS is on). It can open, close and remove
  lots, within limits, but can't touch keys. The owner can rotate or disable
  it at any time without a redeploy.

Every request needs `Authorization: Bearer <key>`. With neither key in play
the whole API answers 404. Wrong keys are counted per client address and
locked out after a handful of attempts. Every change is written to
admin_actions with the role that made it.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import auctions, bidding
from app.auctions import BID_COLUMNS, bid_dict
from app.config import Settings
from app.schemas import AuctionCreate
from app.security import client_ip, key_matches

OWNER, JUDGE = "owner", "judge"


@dataclass(frozen=True)
class Actor:
    role: str
    ip: str


def admin_enabled(settings: Settings) -> bool:
    return settings.admin_key is not None or settings.judge_access


def new_key() -> str:
    return secrets.token_urlsafe(24)  # 32 characters, 192 bits


async def current_judge_key(pool: asyncpg.Pool) -> str | None:
    return await pool.fetchval("SELECT key FROM access_keys WHERE role = 'judge' AND enabled")


async def ensure_judge_key(pool: asyncpg.Pool) -> None:
    """Create the judge key once. Safe when several instances start together."""
    await pool.execute(
        "INSERT INTO access_keys (role, key) VALUES ('judge', $1) ON CONFLICT (role) DO NOTHING", new_key()
    )


async def role_for(state: Any, supplied: str | None) -> str | None:
    settings = state.settings
    if key_matches(supplied, settings.admin_key):
        return OWNER
    if settings.judge_access and key_matches(supplied, await current_judge_key(state.pool)):
        return JUDGE
    return None


async def require_admin(request: Request) -> Actor:
    state = request.app.state
    settings = state.settings
    if not admin_enabled(settings):
        raise HTTPException(404, "not found")
    peer = request.client.host if request.client else None
    ip = client_ip(request.scope.get("headers", []), peer, settings.trusted_proxy_hops)
    if not state.admin_failures.peek(ip):
        raise HTTPException(429, "too many wrong keys from your address; wait a minute")
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    role = await role_for(state, supplied.strip()) if scheme.lower() == "bearer" else None
    if role is None:
        state.admin_failures.allow(ip)
        raise HTTPException(401, "missing or wrong access key", headers={"WWW-Authenticate": "Bearer"})
    return Actor(role, ip)


def _judge_write_allowed(request: Request, actor: Actor) -> None:
    if actor.role == JUDGE and not request.app.state.judge_writes.allow(actor.ip):
        raise HTTPException(429, "the judge key is limited to a few changes a minute; slow down")


def _owner_only(actor: Actor) -> None:
    if actor.role != OWNER:
        raise HTTPException(403, "only the owner key can do this")


async def _audit(pool: asyncpg.Pool, actor: Actor, action: str,
                 auction_id: int | None = None, detail: str = "") -> None:
    await pool.execute(
        "INSERT INTO admin_actions (role, ip, action, auction_id, detail) VALUES ($1, $2, $3, $4, $5)",
        actor.role, actor.ip, action, auction_id, detail[:200],
    )


router = APIRouter(prefix="/admin/api", include_in_schema=False)


@router.get("/overview")
async def overview(request: Request, actor: Actor = Depends(require_admin)) -> dict[str, Any]:
    pool, settings = request.app.state.pool, request.app.state.settings
    lots = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE status = 'open' AND removed_at IS NULL)   AS open,
               count(*) FILTER (WHERE status = 'closed' AND removed_at IS NULL) AS closed,
               count(*) FILTER (WHERE removed_at IS NOT NULL)                   AS removed
        FROM auctions
        """
    )
    bids = await pool.fetchrow(
        """
        SELECT count(*) FILTER (WHERE status = 'accepted')                    AS accepted,
               count(*) FILTER (WHERE status = 'rejected')                    AS rejected,
               count(*) FILTER (WHERE created_at > now() - interval '1 hour') AS last_hour
        FROM bids
        """
    )
    reasons = await pool.fetch(
        "SELECT reason, count(*) AS n FROM bids WHERE status = 'rejected' GROUP BY reason ORDER BY n DESC"
    )
    instances = await pool.fetch(
        """
        SELECT name, started_at, last_seen, websocket_clients,
               now() - last_seen < interval '15 seconds' AS alive
        FROM instances ORDER BY name
        """
    )
    judge = None
    if settings.judge_access:
        row = await pool.fetchrow("SELECT key, enabled, created_at FROM access_keys WHERE role = 'judge'")
        judge = {
            "enabled": bool(row and row["enabled"]),
            # Only the owner sees the key here; judges already have it.
            "key": row["key"] if row and actor.role == OWNER else None,
            "since": row["created_at"].isoformat() if row else None,
        }
    return {
        "you": actor.role,
        "lots": dict(lots),
        "bids": dict(bids),
        "rejected_by_reason": {r["reason"]: r["n"] for r in reasons},
        "instances": [
            {**dict(i), "started_at": i["started_at"].isoformat(), "last_seen": i["last_seen"].isoformat()}
            for i in instances
        ],
        "served_by": settings.instance_name,
        "judge_access": judge,
        "settings": {
            "public_lot_creation": settings.public_lot_creation,
            "demo_restock": settings.demo_restock,
            "rate_limits": settings.rate_limits,
            "unsafe_demo_endpoint": settings.enable_unsafe_demo,
        },
    }


@router.get("/lots")
async def lots(request: Request, actor: Actor = Depends(require_admin)) -> list[dict[str, Any]]:
    return await auctions.list_all(request.app.state.pool, limit=300, include_removed=True)


@router.post("/lots", status_code=201)
async def create_lot(body: AuctionCreate, request: Request,
                     actor: Actor = Depends(require_admin)) -> dict[str, Any]:
    pool, settings = request.app.state.pool, request.app.state.settings
    _judge_write_allowed(request, actor)
    if actor.role == JUDGE:
        open_now = await pool.fetchval(
            "SELECT count(*) FROM auctions WHERE status = 'open' AND removed_at IS NULL"
        )
        if open_now >= settings.judge_open_lot_cap:
            raise HTTPException(409, f"{open_now} lots are already open; close or remove one first")
    lot = await auctions.create(pool, **body.model_dump())
    await _audit(pool, actor, "open lot", lot["id"], lot["title"])
    return lot


@router.post("/lots/{auction_id}/close")
async def close_lot(auction_id: int, request: Request, actor: Actor = Depends(require_admin)) -> dict[str, Any]:
    _judge_write_allowed(request, actor)
    pool = request.app.state.pool
    closed = await bidding.close_now(pool, auction_id)
    if closed is None:
        raise HTTPException(409, "lot not found, already closed, or removed")
    await _audit(pool, actor, "close lot", auction_id,
                 f"{closed['title']}: " + (f"won by {closed['leader']}" if closed["leader"] else "no bids"))
    return closed


@router.post("/lots/{auction_id}/remove")
async def remove_lot(auction_id: int, request: Request, actor: Actor = Depends(require_admin)) -> dict[str, Any]:
    _judge_write_allowed(request, actor)
    pool = request.app.state.pool
    removed = await bidding.remove(pool, auction_id)
    if removed is None:
        raise HTTPException(409, "lot not found or already removed")
    await _audit(pool, actor, "remove lot", auction_id, removed["title"])
    return removed


@router.get("/bids")
async def bid_log(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    rejected_only: bool = False,
    actor: Actor = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Every bid attempt, newest first, including rejected ones and their request ids."""
    rows = await request.app.state.pool.fetch(
        f"""
        SELECT {", ".join("b." + c.strip() for c in BID_COLUMNS.split(","))}, a.title
        FROM bids b JOIN auctions a ON a.id = b.auction_id
        WHERE NOT $2 OR b.status = 'rejected'
        ORDER BY b.id DESC LIMIT $1
        """,
        limit,
        rejected_only,
    )
    return [{**bid_dict(r, private=True), "title": r["title"]} for r in rows]


@router.get("/activity")
async def activity(request: Request, limit: int = Query(50, ge=1, le=200),
                   actor: Actor = Depends(require_admin)) -> list[dict[str, Any]]:
    """Who changed what. Client addresses are shown to the owner only."""
    rows = await request.app.state.pool.fetch(
        "SELECT at, role, ip, action, auction_id, detail FROM admin_actions ORDER BY id DESC LIMIT $1", limit
    )
    return [
        {"at": r["at"].isoformat(), "role": r["role"], "action": r["action"], "auction_id": r["auction_id"],
         "detail": r["detail"], "ip": r["ip"] if actor.role == OWNER else None}
        for r in rows
    ]


@router.post("/judge-key/rotate")
async def rotate_judge_key(request: Request, actor: Actor = Depends(require_admin)) -> dict[str, Any]:
    """Issue a new judge key. The old one stops working on the next request."""
    _owner_only(actor)
    pool = request.app.state.pool
    key = new_key()
    await pool.execute(
        """
        INSERT INTO access_keys (role, key) VALUES ('judge', $1)
        ON CONFLICT (role) DO UPDATE SET key = EXCLUDED.key, enabled = true, created_at = now()
        """,
        key,
    )
    await _audit(pool, actor, "rotate judge key")
    return {"enabled": True, "key": key}


@router.post("/judge-key/{state}")
async def set_judge_key(state: str, request: Request, actor: Actor = Depends(require_admin)) -> dict[str, Any]:
    _owner_only(actor)
    if state not in ("enable", "disable"):
        raise HTTPException(404, "not found")
    pool = request.app.state.pool
    await pool.execute("UPDATE access_keys SET enabled = $1 WHERE role = 'judge'", state == "enable")
    await _audit(pool, actor, f"{state} judge key")
    return {"enabled": state == "enable"}
