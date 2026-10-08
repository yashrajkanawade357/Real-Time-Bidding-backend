"""The bid transaction: rules, idempotency, and correctness under real concurrency."""

from __future__ import annotations

import asyncio
import random

import pytest

from app import auctions, bidding
from tests.conftest import assert_consistent


async def _auction(pool, *, start=100, inc=1, seconds=300):
    return await auctions.create(
        pool, title="Test lot", description="", starting_price=start,
        min_increment=inc, duration_seconds=seconds,
    )


async def test_first_bid_must_meet_starting_price(pool):
    a = await _auction(pool, start=100)
    low = await bidding.place_bid(pool, a["id"], "alice", 99, "r1")
    ok = await bidding.place_bid(pool, a["id"], "alice", 100, "r2")
    assert (low.accepted, low.reason) == (False, bidding.TOO_LOW)
    assert ok.accepted and ok.auction["current_price"] == 100


async def test_bid_must_beat_current_price_by_increment(pool):
    a = await _auction(pool, start=100, inc=10)
    await bidding.place_bid(pool, a["id"], "alice", 100, "r1")
    short = await bidding.place_bid(pool, a["id"], "bob", 109, "r2")
    enough = await bidding.place_bid(pool, a["id"], "bob", 110, "r3")
    assert (short.accepted, short.reason) == (False, bidding.TOO_LOW)
    assert enough.accepted
    assert enough.auction["leader"] == "bob" and enough.auction["min_next_bid"] == 120
    await assert_consistent(pool, a["id"])


async def test_rejected_bids_are_recorded_but_change_nothing(pool):
    a = await _auction(pool)
    await bidding.place_bid(pool, a["id"], "alice", 150, "r1")
    rejected = await bidding.place_bid(pool, a["id"], "bob", 120, "r2")
    assert not rejected.accepted
    assert rejected.auction["version"] == 1  # untouched by the rejected bid
    rows = await auctions.bid_history(pool, a["id"], limit=10, include_rejected=True)
    assert [(r["bidder"], r["status"]) for r in rows] == [("bob", "rejected"), ("alice", "accepted")]


async def test_same_request_id_is_applied_once(pool):
    a = await _auction(pool)
    first = await bidding.place_bid(pool, a["id"], "alice", 200, "same-id")
    again = await bidding.place_bid(pool, a["id"], "alice", 200, "same-id")
    assert first.accepted and not first.duplicate
    assert again.accepted and again.duplicate and again.bid["id"] == first.bid["id"]
    assert again.auction["bid_count"] == 1


async def test_concurrent_bids_highest_always_wins(pool):
    """300 bids fired at once, in random order. Whatever order Postgres sees
    them in, the highest must win and the accepted sequence must only climb."""
    for seed in (1, 2, 3):
        a = await _auction(pool, start=100, inc=1)
        amounts = list(range(100, 400))
        random.Random(seed).shuffle(amounts)

        outcomes = await asyncio.gather(*(
            bidding.place_bid(pool, a["id"], f"bidder-{i % 23}", amount, f"req-{i}")
            for i, amount in enumerate(amounts)
        ))

        accepted = [o for o in outcomes if o.accepted]
        snap = await auctions.snapshot(pool, a["id"])
        assert snap["auction"]["current_price"] == 399  # the max bid always wins
        assert snap["auction"]["version"] == len(accepted)
        assert all(o.reason == bidding.TOO_LOW for o in outcomes if not o.accepted)
        assert await pool.fetchval("SELECT count(*) FROM bids WHERE auction_id = $1", a["id"]) == 300
        await assert_consistent(pool, a["id"])


async def test_equal_simultaneous_bids_only_one_wins(pool):
    a = await _auction(pool, start=100, inc=5)
    outcomes = await asyncio.gather(*(
        bidding.place_bid(pool, a["id"], f"bidder-{i}", 500, f"req-{i}") for i in range(50)
    ))
    winners = [o for o in outcomes if o.accepted]
    assert len(winners) == 1
    snap = await auctions.snapshot(pool, a["id"])
    assert snap["auction"]["leader"] == winners[0].bid["bidder"]
    await assert_consistent(pool, a["id"])


async def test_concurrent_retries_of_one_request_create_one_bid(pool):
    """A flaky client resending the same bid 25 times at once still bids once."""
    a = await _auction(pool)
    outcomes = await asyncio.gather(*(
        bidding.place_bid(pool, a["id"], "alice", 300, "retry-me") for _ in range(25)
    ))
    assert sum(not o.duplicate for o in outcomes) == 1
    assert len({o.bid["id"] for o in outcomes}) == 1
    assert await pool.fetchval("SELECT count(*) FROM bids WHERE auction_id = $1", a["id"]) == 1
    await assert_consistent(pool, a["id"])


async def test_bids_on_different_auctions_dont_interfere(pool):
    lots = [await _auction(pool, start=10) for _ in range(5)]
    await asyncio.gather(*(
        bidding.place_bid(pool, lot["id"], f"b{i}", 10 + i, f"{lot['id']}-{i}")
        for lot in lots for i in range(40)
    ))
    for lot in lots:
        snap = await auctions.snapshot(pool, lot["id"])
        assert snap["auction"]["current_price"] == 49
        await assert_consistent(pool, lot["id"])


async def test_unknown_auction(pool):
    with pytest.raises(bidding.AuctionNotFound):
        await bidding.place_bid(pool, 999_999, "alice", 100, "r1")
