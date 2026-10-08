"""HTTP API, app wiring and lifecycle.

Run:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import auctions, bidding, db, demo
from app.config import MAX_AMOUNT, ROOT, Settings, load_settings
from app.events import EventListener
from app.hub import Hub
from app.realtime import router as realtime_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC_DIR = ROOT / "static"
# Browsers re-check the client files on every load (a cheap 304 when unchanged),
# so a redeploy is picked up immediately - there is no build step to hash names.
REVALIDATE = {"Cache-Control": "no-cache"}


class RevalidatedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers.update(REVALIDATE)
        return response


class AuctionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    starting_price: int = Field(ge=1, le=MAX_AMOUNT)
    min_increment: int = Field(default=1, ge=1, le=MAX_AMOUNT)
    duration_seconds: int = Field(default=300, ge=5, le=7 * 24 * 3600)


class BidIn(BaseModel):
    bidder: str = Field(min_length=1, max_length=40)
    amount: int = Field(ge=1, le=MAX_AMOUNT)
    # Optional idempotency key. Send the same one when retrying a bid whose
    # response you never got, and you'll get the original outcome back.
    request_id: str | None = Field(default=None, min_length=1, max_length=64)


async def _resync_all(app: FastAPI) -> None:
    """Push a fresh snapshot to everyone connected to this instance."""
    pool, hub = app.state.pool, app.state.hub
    for auction_id in hub.auction_ids():
        snapshot = await auctions.snapshot_message(pool, auction_id)
        if snapshot is not None:
            hub.publish(auction_id, snapshot)


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
        ]
        if settings.demo_restock:
            background.append(asyncio.create_task(demo.restock_loop(pool, settings.demo_open_lots)))
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
        version="1.0.0",
        description="Live auctions over WebSockets. Postgres row locks decide every bid.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(realtime_router)
    app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers=REVALIDATE)

    @app.get("/health")
    async def health(request: Request) -> dict:
        state = request.app.state
        db_ok = await state.pool.fetchval("SELECT 1") == 1
        return {
            "status": "ok" if db_ok and state.listener.connected else "degraded",
            "database": db_ok,
            "event_listener": state.listener.connected,
            "websocket_clients": state.hub.client_count(),
        }

    @app.post("/auctions", status_code=201)
    async def create_auction(body: AuctionCreate, request: Request) -> dict:
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
            503: {"description": "Auction busy - retry with the same request_id"},
        },
    )
    async def post_bid(auction_id: int, body: BidIn, request: Request) -> JSONResponse:
        try:
            outcome = await bidding.place_bid(
                request.app.state.pool,
                auction_id,
                body.bidder.strip(),
                body.amount,
                body.request_id or uuid.uuid4().hex,
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
                request.app.state.pool, auction_id, body.bidder.strip(), body.amount,
                settings.unsafe_delay_ms,
            )
        except bidding.AuctionNotFound:
            raise HTTPException(404, "auction not found") from None
        return _bid_response(outcome)

    return app


app = create_app()
