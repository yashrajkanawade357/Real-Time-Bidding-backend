"""Connection pool and schema migrations."""

from __future__ import annotations

import logging

import asyncpg

from app.config import ROOT, Settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = ROOT / "migrations"

# Arbitrary constant: every instance takes this advisory lock before migrating,
# so N instances booting at once apply each migration exactly once.
_MIGRATION_LOCK_ID = 7_270_001


async def create_pool(settings: Settings) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.pool_min,
        max_size=settings.pool_max,
        command_timeout=15,
        server_settings={
            "application_name": "bidding-api",
            # A bid waits at most this long for the auction row lock. Under
            # extreme contention the client gets "busy, retry" instead of a
            # request that hangs forever.
            "lock_timeout": "5s",
        },
    )


async def migrate(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name       TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            applied = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in applied:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
                log.info("applied migration %s", path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID)
