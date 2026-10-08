"""HTTP API, app wiring and lifecycle.

Run:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import admin, auctions, auth, bidding, db, demo
from app.config import ROOT, Settings, load_settings
from app.events import EventListener
from app.hub import Hub
from app.realtime import SOCKET_BID_BURST, SOCKET_BIDS_PER_SEC
from app.realtime import router as realtime_router
from app.schemas import AuctionCreate, BidIn
from app.security import RateLimiter, SecurityMiddleware, client_ip

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

STATIC_DIR = ROOT / "static"
# Browsers re-check the client files on every load (a cheap 304 when unchanged),
# so a redeploy is picked up immediately - there is no build step to hash names.
REVALIDATE = {"Cache-Control": "no-cache"}
HEARTBEAT_SECONDS = 5


class RevalidatedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.update(REVALIDATE)
        return response


async def _resync_all(app: FastAPI) -> None:
    """After the event feed reconnects: fresh snapshots for everyone on this instance."""
    pool, hub, name = app.state.pool, app.state.hub, app.state.settings.instance_name
    for auction_id in hub.auction_ids():
        snapshot = await auctions.snapshot_message(pool, auction_id, instance=name)
        if snapshot is not None:
            hub.publish(auction_id, snapshot)
    hub.publish_admins(json.dumps({"type": "feed_gap", "instance": name}))


async def _heartbeat(app: FastAPI) -> None:
    """Report this instance's liveness and socket count for the admin portal."""
    pool, hub, name = app.state.pool, app.state.hub, app.state.settings.instance_name
    started = datetime.now(UTC)
    while True:
        try:
            await pool.execute(
                """
                INSERT INTO instances (name, started_at, last_seen, websocket_clients)
                VALUES ($1, $2, now(), $3)
                ON CONFLICT (name) DO UPDATE
                SET started_at = EXCLUDED.started_at, last_seen = now(),
                    websocket_clients = EXCLUDED.websocket_clients
                """,
                name, started, hub.client_count(),
            )
            await pool.execute("DELETE FROM instances WHERE last_seen < now() - interval '1 day'")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("heartbeat failed")
        await asyncio.sleep(HEARTBEAT_SECONDS)


def _bid_response(outcome: bidding.BidOutcome) -> JSONResponse:
    if outcome.accepted:
        status = 200 if outcome.duplicate else 201
    else:
        status = 409
    return JSONResponse(outcome.as_dict(), status_code=status)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = await db.create_pool(settings)
        await db.migrate(pool)
        if settings.judge_access:
            await admin.ensure_judge_key(pool)
        hub = Hub(settings.client_queue_max)
        app.state.pool, app.state.hub = pool, hub
        app.state.listener = EventListener(
            settings.database_url,
            on_event=hub.publish,
            on_resync=lambda: _resync_all(app),
            healthcheck_interval=settings.listener_healthcheck,
        )
        background = [
            asyncio.create_task(app.state.listener.run()),
            asyncio.create_task(bidding.closer_loop(pool, settings.closer_interval)),
            asyncio.create_task(_heartbeat(app)),
        ]
        if settings.demo_restock:
            background.append(asyncio.create_task(demo.restock_loop(pool, settings.demo_open_lots)))
        if not admin.admin_enabled(settings):
            log.info("admin portal is off (no ADMIN_KEY, no JUDGE_ACCESS)")
        try:
            yield
        finally:
            hub.close_all()  # clients reconnect - to this instance once it's back, or another
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            try:
                await asyncio.wait_for(pool.close(), timeout=5)
            except TimeoutError:
                pool.terminate()

    app = FastAPI(
        title="Real-Time Bidding API",
        version="1.1.0",
        description="Live auctions over WebSockets. Postgres row locks decide every bid.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.bid_limiter = RateLimiter(settings.bid_rate_per_sec, settings.bid_burst,
                                        enabled=settings.rate_limits)
    app.state.lot_limiter = RateLimiter(5 / 60, 5, enabled=settings.rate_limits)
    app.state.socket_bid_limiter = RateLimiter(SOCKET_BIDS_PER_SEC, SOCKET_BID_BURST,
                                               enabled=settings.rate_limits)
    # Always on: 10 wrong admin keys, then one more try every 6 seconds.
    app.state.admin_failures = RateLimiter(10 / 60, 10)
    app.state.signup_limiter = RateLimiter(settings.signups_per_hour / 3600, settings.signups_per_hour,
                                           enabled=settings.rate_limits)
    # Always on: 10 wrong passwords, then one more try every 6 seconds.
    app.state.login_failures = RateLimiter(10 / 60, 10)
    # The shareable judge key gets a budget of changes per minute per address.
    app.state.judge_writes = RateLimiter(settings.judge_writes_per_min / 60, settings.judge_writes_per_min)

    app.add_middleware(SecurityMiddleware, instance=settings.instance_name,
                       max_body_bytes=settings.max_body_bytes)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )
    app.include_router(realtime_router)
    app.include_router(admin.router)
    app.include_router(auth.router)
    app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")

    def ip_of(request: Request) -> str:
        peer = request.client.host if request.client else None
        return client_ip(request.scope.get("headers", []), peer, settings.trusted_proxy_hops)

    def page(name: str) -> FileResponse:
        return FileResponse(STATIC_DIR / name, headers=REVALIDATE)

    @app.get("/", include_in_schema=False)
    async def landing() -> FileResponse:
        return page("landing.html")

    @app.get("/floor", include_in_schema=False)
    async def floor() -> FileResponse:
        return page("floor.html")

    @app.get("/admin", include_in_schema=False)
    async def admin_portal() -> FileResponse:
        return page("admin.html")

    @app.get("/login", include_in_schema=False)
    async def login_page() -> FileResponse:
        return page("login.html")

    @app.get("/health")
    async def health(request: Request) -> dict:
        state = request.app.state
        db_ok = await state.pool.fetchval("SELECT 1") == 1
        return {
            "status": "ok" if db_ok and state.listener.connected else "degraded",
            "database": db_ok,
            "event_listener": state.listener.connected,
            "websocket_clients": state.hub.client_count(),
            "instance": settings.instance_name,
        }

    @app.get("/config")
    async def client_config(request: Request) -> dict:
        """What the browser pages need to know about this deployment. With
        JUDGE_ACCESS on, that includes the judge key: it's meant to be public."""
        judge_key = await admin.current_judge_key(request.app.state.pool) if settings.judge_access else None
        return {
            "public_lot_creation": settings.public_lot_creation,
            "instance": settings.instance_name,
            "judge_key": judge_key,
            "require_login": settings.require_login,
            "soft_close_seconds": settings.soft_close_seconds,
        }

    @app.post("/auctions", status_code=201)
    async def create_auction(body: AuctionCreate, request: Request) -> dict:
        if not settings.public_lot_creation:
            raise HTTPException(403, "lots are opened by the auctioneer on this server")
        if not request.app.state.lot_limiter.allow(ip_of(request)):
            raise HTTPException(429, "too many new lots from your address; try again shortly")
        return await auctions.create(request.app.state.pool, **body.model_dump())

    @app.get("/auctions")
    async def list_auctions(request: Request) -> list[dict]:
        return await auctions.list_all(request.app.state.pool)

    @app.get("/auctions/{auction_id}")
    async def get_auction(auction_id: int, request: Request) -> dict:
        snap = await auctions.snapshot(request.app.state.pool, auction_id)
        if snap is None:
            raise HTTPException(404, "auction not found")
        return snap

    @app.get("/auctions/{auction_id}/bids")
    async def get_bids(
        auction_id: int,
        request: Request,
        limit: int = Query(50, ge=1, le=500),
        include_rejected: bool = False,
    ) -> list[dict]:
        bids = await auctions.bid_history(
            request.app.state.pool, auction_id, limit=limit, include_rejected=include_rejected
        )
        if bids is None:
            raise HTTPException(404, "auction not found")
        return bids

    @app.post(
        "/auctions/{auction_id}/bids",
        responses={
            201: {"description": "Accepted - this is now the high bid"},
            200: {"description": "Duplicate request_id - the original accepted outcome"},
            409: {"description": "Rejected (bid_too_low, auction_closed)"},
            429: {"description": "Too many bids from this address; slow down"},
            503: {"description": "Auction busy - retry with the same request_id"},
        },
    )
    async def post_bid(auction_id: int, body: BidIn, request: Request) -> JSONResponse:
        auth.require_same_origin(request)
        if not request.app.state.bid_limiter.allow(ip_of(request)):
            raise HTTPException(429, "too many bids from your address; slow down")
        if settings.require_login:
            # Who is bidding comes from the session, never from the request body.
            user = await auth.current_user(request)
            if user is None:
                raise HTTPException(401, "log in to bid")
            bidder, user_id = user.display_name, user.id
        else:
            if not body.bidder or not body.bidder.strip():
                raise HTTPException(422, "bidder is required")
            bidder, user_id = body.bidder.strip(), None
        try:
            outcome = await bidding.place_bid(
                request.app.state.pool,
                auction_id,
                bidder,
                body.amount,
                body.request_id or uuid.uuid4().hex,
                user_id=user_id,
                soft_close=settings.soft_close_seconds,
            )
        except bidding.AuctionNotFound:
            raise HTTPException(404, "auction not found") from None
        except bidding.AuctionBusy:
            raise HTTPException(503, "auction busy, retry with the same request_id") from None
        return _bid_response(outcome)

    @app.post("/auctions/{auction_id}/bids/unsafe", include_in_schema=False)
    async def post_bid_unsafe(auction_id: int, body: BidIn, request: Request) -> JSONResponse:
        if not settings.enable_unsafe_demo:
            raise HTTPException(404, "not found")
        try:
            outcome = await bidding.place_bid_unsafe(
                request.app.state.pool, auction_id, (body.bidder or "anonymous").strip(), body.amount,
                settings.unsafe_delay_ms,
            )
        except bidding.AuctionNotFound:
            raise HTTPException(404, "auction not found") from None
        return _bid_response(outcome)

    return app


app = create_app()
