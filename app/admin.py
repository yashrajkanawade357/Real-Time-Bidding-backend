"""Admin API behind the access key: /admin/api/*

Every route requires `Authorization: Bearer <ADMIN_KEY>`. With no key
configured the whole API answers 404, as if it didn't exist. Wrong keys are
counted per client address and locked out after a handful of attempts.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import auctions, bidding
from app.auctions import BID_COLUMNS, bid_dict
from app.schemas import AuctionCreate
from app.security import client_ip, key_matches


async def require_admin(request: Request) -> None:
    state = request.app.state
    settings = state.settings
    if settings.admin_key is None:
        raise HTTPException(404, "not found")
    peer = request.client.host if request.client else None
    ip = client_ip(request.scope.get("headers", []), peer, settings.trusted_proxy_hops)
    if not state.admin_failures.peek(ip):
        raise HTTPException(429, "too many wrong keys from your address; wait a minute")
    scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not key_matches(supplied.strip(), settings.admin_key):
        state.admin_failures.allow(ip)
        raise HTTPException(401, "missing or wrong access key", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(prefix="/admin/api", dependencies=[Depends(require_admin)], include_in_schema=False)


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
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
    return {
        "lots": dict(lots),
        "bids": dict(bids),
        "rejected_by_reason": {r["reason"]: r["n"] for r in reasons},
        "instances": [
            {**dict(i), "started_at": i["started_at"].isoformat(), "last_seen": i["last_seen"].isoformat()}
            for i in instances
        ],
        "served_by": settings.instance_name,
        "settings": {
            "public_lot_creation": settings.public_lot_creation,
            "demo_restock": settings.demo_restock,
            "rate_limits": settings.rate_limits,
            "unsafe_demo_endpoint": settings.enable_unsafe_demo,
        },
    }


@router.get("/lots")
async def lots(request: Request) -> list[dict[str, Any]]:
    return await auctions.list_all(request.app.state.pool, limit=300, include_removed=True)


@router.post("/lots", status_code=201)
async def create_lot(body: AuctionCreate, request: Request) -> dict[str, Any]:
    return await auctions.create(request.app.state.pool, **body.model_dump())


@router.post("/lots/{auction_id}/close")
async def close_lot(auction_id: int, request: Request) -> dict[str, Any]:
    closed = await bidding.close_now(request.app.state.pool, auction_id)
    if closed is None:
        raise HTTPException(409, "lot not found, already closed, or removed")
    return closed


@router.post("/lots/{auction_id}/remove")
async def remove_lot(auction_id: int, request: Request) -> dict[str, Any]:
    removed = await bidding.remove(request.app.state.pool, auction_id)
    if removed is None:
        raise HTTPException(409, "lot not found or already removed")
    return removed


@router.get("/bids")
async def bid_log(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    rejected_only: bool = False,
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
