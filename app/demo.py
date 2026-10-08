"""Keeps a public demo stocked with open lots, so whoever opens the link always
finds something live to bid on. Off unless DEMO_RESTOCK=true.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, NamedTuple

import asyncpg

from app.auctions import AUCTION_COLUMNS, auction_dict

log = logging.getLogger(__name__)

# Every instance restocks on a timer; this lock makes them take turns.
_RESTOCK_LOCK_ID = 7_270_002


class Lot(NamedTuple):
    title: str
    description: str
    starting_price: int
    min_increment: int


CATALOGUE: tuple[Lot, ...] = (
    Lot("Leica M6 rangefinder, 1986",
        "Black chrome body, serviced last year. Meter reads accurately and every shutter "
        "speed has been tested. Light brassing on the top plate.", 85_000, 2_500),
    Lot("Omega Seamaster 300, 1967",
        "Reference 165.024 on its original dial and hands. Keeps good time; last serviced "
        "in 2021. Supplied without box or papers.", 1_20_000, 5_000),
    Lot("Olivetti Lettera 32 typewriter",
        "Original zip case and a new ribbon. Types cleanly across all keys; minor paint wear "
        "on the carriage return lever.", 6_500, 250),
    Lot("Pair of Bidriware vases, Bidar",
        "Silver inlay on blackened zinc alloy, 24 cm. Hand-made in Bidar, Karnataka. "
        "One base has a small dent.", 18_000, 1_000),
    Lot("HMT Janata wristwatch, 1980s",
        "Hand-wound, 17 jewels, cream dial. Recently cleaned; new leather strap.", 3_200, 100),
    Lot("Nikon F3 with Nikkor 50mm f/1.4",
        "Body and lens in working order. Viewfinder is clean, with a few specks of dust "
        "on the focusing screen.", 32_000, 1_000),
    Lot("Teak campaign chest, c. 1900",
        "Two-part chest with brass corners and recessed handles. Refinished; one replacement "
        "drawer pull.", 75_000, 2_500),
    Lot("Hand-painted film poster, 1970s",
        "Original hand-painted cinema hoarding panel on board, 90 x 60 cm. Colours bright; "
        "edges worn.", 12_000, 500),
)


async def restock(pool: asyncpg.Pool, minimum: int, rng: random.Random | None = None) -> list[dict[str, Any]]:
    """Open catalogue lots until at least `minimum` lots are open. Safe to run
    from every instance at once: the advisory lock makes them take turns, and
    each one re-counts inside the lock."""
    rng = rng or random.Random()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _RESTOCK_LOCK_ID)
        open_titles = {
            r["title"]
            for r in await conn.fetch(
                "SELECT title FROM auctions WHERE status = 'open' AND ends_at > now()"
            )
        }
        missing = minimum - len(open_titles)
        if missing <= 0:
            return []
        choices = [lot for lot in CATALOGUE if lot.title not in open_titles]
        rng.shuffle(choices)
        created = []
        for lot in choices[:missing]:
            row = await conn.fetchrow(
                f"""
                INSERT INTO auctions (title, description, starting_price, min_increment, ends_at)
                VALUES ($1, $2, $3, $4, now() + make_interval(mins => $5))
                RETURNING {AUCTION_COLUMNS}
                """,
                lot.title, lot.description, lot.starting_price, lot.min_increment,
                rng.randint(8, 40),
            )
            created.append(auction_dict(row))
        return created


async def restock_loop(pool: asyncpg.Pool, minimum: int, interval: float = 20.0) -> None:
    while True:
        try:
            created = await restock(pool, minimum)
            if created:
                log.info("demo: opened %d lots", len(created))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("demo restock failed")
        await asyncio.sleep(interval)
