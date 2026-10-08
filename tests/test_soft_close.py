"""Anti-sniping: a bid in the last moments pushes the end back."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app import auctions, bidding
from tests.conftest import assert_consistent

WINDOW = 30


async def _ending_soon(pool, seconds_left: float):
    a = await auctions.create(pool, title="Ending soon", description="", starting_price=100,
                              min_increment=10, duration_seconds=600)
    await pool.execute(
        "UPDATE auctions SET ends_at = now() + make_interval(secs => $2) WHERE id = $1", a["id"], seconds_left
    )
    return a


async def test_a_late_bid_extends_the_auction(pool):
    a = await _ending_soon(pool, 5)
    before = await pool.fetchval("SELECT ends_at FROM auctions WHERE id = $1", a["id"])
    out = await bidding.place_bid(pool, a["id"], "asha", 100, "r1", soft_close=WINDOW)
    after = await pool.fetchval("SELECT ends_at FROM auctions WHERE id = $1", a["id"])
    assert out.accepted and out.auction["extensions"] == 1
    assert after - before > timedelta(seconds=WINDOW - 6)  # now about 30 s away instead of 5


async def test_an_early_bid_does_not(pool):
    a = await _ending_soon(pool, 300)
    before = await pool.fetchval("SELECT ends_at FROM auctions WHERE id = $1", a["id"])
    out = await bidding.place_bid(pool, a["id"], "asha", 100, "r1", soft_close=WINDOW)
    assert out.auction["extensions"] == 0
    assert await pool.fetchval("SELECT ends_at FROM auctions WHERE id = $1", a["id"]) == before


async def test_rejected_late_bids_do_not_extend(pool):
    a = await _ending_soon(pool, 5)
    await bidding.place_bid(pool, a["id"], "asha", 100, "r1", soft_close=WINDOW)
    ends = await pool.fetchval("SELECT ends_at FROM auctions WHERE id = $1", a["id"])
    low = await bidding.place_bid(pool, a["id"], "kabir", 100, "r2", soft_close=WINDOW)  # too low
    assert not low.accepted
    assert await pool.fetchval("SELECT ends_at FROM auctions WHERE id = $1", a["id"]) == ends


async def test_a_bid_after_the_original_deadline_counts_once_extended(pool):
    a = await _ending_soon(pool, 1)
    await bidding.place_bid(pool, a["id"], "asha", 100, "r1", soft_close=WINDOW)
    await asyncio.sleep(1.5)  # past the ORIGINAL end, inside the extension
    late = await bidding.place_bid(pool, a["id"], "kabir", 110, "r2", soft_close=WINDOW)
    assert late.accepted
    assert await bidding.close_expired(pool) == []  # not over yet


async def test_concurrent_late_bids_stay_consistent(pool):
    a = await _ending_soon(pool, 3)
    outcomes = await asyncio.gather(*(
        bidding.place_bid(pool, a["id"], f"b{i}", 100 + i * 10, f"r{i}", soft_close=WINDOW) for i in range(50)
    ))
    accepted = sum(o.accepted for o in outcomes)
    row = await pool.fetchrow("SELECT extensions, ends_at - now() AS left FROM auctions WHERE id = $1", a["id"])
    assert row["extensions"] == accepted  # every accepted bid was inside the window
    assert row["left"] > timedelta(seconds=WINDOW - 5)
    await assert_consistent(pool, a["id"])
