"""Auctions end on time, exactly once, and refuse bids afterwards."""

from __future__ import annotations

import asyncio

from app import auctions, bidding
from tests.conftest import assert_consistent


async def _expired_auction(pool):
    a = await auctions.create(
        pool, title="Ending", description="", starting_price=100,
        min_increment=1, duration_seconds=300,
    )
    await bidding.place_bid(pool, a["id"], "alice", 150, "r1")
    await pool.execute("UPDATE auctions SET ends_at = now() - interval '1 second' WHERE id = $1", a["id"])
    return a


async def test_bid_after_end_time_is_rejected_even_before_closer_runs(pool):
    a = await _expired_auction(pool)
    late = await bidding.place_bid(pool, a["id"], "bob", 10_000, "r2")
    assert (late.accepted, late.reason) == (False, bidding.CLOSED)
    snap = await auctions.snapshot(pool, a["id"])
    assert snap["auction"]["leader"] == "alice"


async def test_racing_closers_close_each_auction_exactly_once(pool):
    """Every API instance runs the closer. Five of them racing must close the
    auction once, bump its version once, and announce it once."""
    a = await _expired_auction(pool)
    results = await asyncio.gather(*(bidding.close_expired(pool) for _ in range(5)))
    closed = [auction for batch in results for auction in batch]
    assert [c["id"] for c in closed] == [a["id"]]
    snap = await auctions.snapshot(pool, a["id"])
    assert snap["auction"]["status"] == "closed"
    assert snap["auction"]["version"] == 2  # one bid + one close
    assert snap["auction"]["leader"] == "alice"  # the winner


async def test_closed_auction_rejects_bids(pool):
    a = await _expired_auction(pool)
    await bidding.close_expired(pool)
    late = await bidding.place_bid(pool, a["id"], "bob", 10_000, "r2")
    assert late.reason == bidding.CLOSED
    await assert_consistent(pool, a["id"])
