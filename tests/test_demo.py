"""Demo restocking: tops up open lots exactly once, even with instances racing."""

from __future__ import annotations

import asyncio

from app import auctions, demo


async def test_racing_instances_restock_exactly_to_the_minimum(pool):
    results = await asyncio.gather(*(demo.restock(pool, 4) for _ in range(6)))
    assert sum(len(created) for created in results) == 4
    titles = [r["title"] for r in await pool.fetch("SELECT title FROM auctions WHERE status = 'open'")]
    assert len(titles) == 4 and len(set(titles)) == 4
    assert await demo.restock(pool, 4) == []


async def test_restock_counts_lots_that_are_already_open(pool):
    await auctions.create(pool, title="Someone's own lot", description="", starting_price=100,
                          min_increment=10, duration_seconds=600)
    assert len(await demo.restock(pool, 3)) == 2
