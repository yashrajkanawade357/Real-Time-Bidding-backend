"""Postgres LISTEN/NOTIFY: how a committed change reaches every API instance.

The bid transaction calls pg_notify() just before COMMIT. Postgres delivers the
notification to every connection LISTENing on the channel - one per API
instance - only once the transaction has committed. So:

* a client connected to instance B hears about a bid placed on instance A,
  with no sticky sessions and no extra broker;
* nobody ever hears about a bid that rolled back;
* notifications arrive in commit order.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import asyncpg

log = logging.getLogger(__name__)

CHANNEL = "auction_events"


class EventListener:
    """One dedicated connection that LISTENs and hands each event to `on_event`.

    If that connection drops, Postgres does not replay what was missed, so after
    every reconnect we call `on_resync`, which pushes a fresh snapshot to every
    connected client. Clients may get an extra snapshot; they never stay stale.
    """

    def __init__(
        self,
        dsn: str,
        on_event: Callable[[int, str], None],
        on_resync: Callable[[], Awaitable[None]],
        healthcheck_interval: float = 5.0,
    ) -> None:
        self._dsn = dsn
        self._on_event = on_event
        self._on_resync = on_resync
        self._healthcheck = healthcheck_interval
        self.connected = False

    def _dispatch(self, _conn: object, _pid: int, _channel: str, payload: str) -> None:
        try:
            auction_id = json.loads(payload)["auction"]["id"]
        except (ValueError, KeyError, TypeError):
            log.warning("ignoring malformed notification: %.200s", payload)
            return
        # Forward the payload as-is: it is already the JSON clients receive.
        self._on_event(auction_id, payload)

    async def run(self) -> None:
        backoff = 0.5
        while True:
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(
                    self._dsn, server_settings={"application_name": "bidding-listener"}
                )
                lost = asyncio.Event()
                conn.add_termination_listener(lambda _c: lost.set())
                await conn.add_listener(CHANNEL, self._dispatch)
                self.connected = True
                backoff = 0.5
                log.info("listening on %s", CHANNEL)
                await self._on_resync()

                while not lost.is_set():
                    try:
                        await asyncio.wait_for(lost.wait(), timeout=self._healthcheck)
                    except TimeoutError:
                        # Catches a silently dead TCP connection, which never
                        # fires the termination listener.
                        await conn.execute("SELECT 1")
                raise ConnectionError("listener connection closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("listener down (%s); reconnecting in %.1fs", exc, backoff)
            finally:
                self.connected = False
                if conn is not None and not conn.is_closed():
                    conn.terminate()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)
