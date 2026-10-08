"""Who is connected to which auction on this instance, and pushing messages to them.

The hub holds no auction state - only sockets. Postgres is the source of truth,
so a client that connects, drops or reconnects can never corrupt anything:
disconnecting just removes a socket from a set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from fastapi import WebSocket

log = logging.getLogger(__name__)

SLOW_CONSUMER = 4008
SERVER_RESTART = 1012


class Client:
    """One WebSocket. Outgoing messages go through a bounded queue so a slow
    client can't stall broadcasts to everyone else - if its queue fills up it
    is disconnected, and on reconnect it gets a fresh snapshot."""

    def __init__(
        self, ws: WebSocket, auction_id: int, bidder: str | None, queue_max: int, ip: str = ""
    ) -> None:
        self.ws = ws
        self.auction_id = auction_id
        self.bidder = bidder
        self.ip = ip
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_max)
        self._closing = asyncio.Event()
        self._close_args: tuple[int, str] | None = None

    def offer(self, text: str) -> None:
        if self._closing.is_set():
            return
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            log.warning("auction %s: dropping slow client %s", self.auction_id, self.bidder)
            self.close(SLOW_CONSUMER, "too slow; reconnect for a fresh snapshot")

    def close(self, code: int, reason: str = "") -> None:
        if not self._closing.is_set():
            self._close_args = (code, reason)
            self._closing.set()

    async def _pump(self) -> None:
        while True:
            await self.ws.send_text(await self._queue.get())

    async def _read(self, on_message: Callable[[Client, str], Awaitable[None]]) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if text is None:
                text = (message.get("bytes") or b"").decode("utf-8", errors="replace")
            await on_message(self, text)

    async def serve(self, on_message: Callable[[Client, str], Awaitable[None]]) -> None:
        """Run until the client disconnects, the socket fails, or we close it."""
        tasks = {
            asyncio.create_task(self._pump()),
            asyncio.create_task(self._read(on_message)),
            asyncio.create_task(self._closing.wait()),
        }
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._close_args is not None:
                with contextlib.suppress(Exception):
                    await self.ws.close(*self._close_args)


class Hub:
    def __init__(self, queue_max: int) -> None:
        self.queue_max = queue_max
        self._rooms: dict[int, set[Client]] = defaultdict(set)
        self._admins: set[Client] = set()  # receive every event, for every lot
        self._per_ip: dict[str, int] = defaultdict(int)

    def join(self, client: Client) -> None:
        self._rooms[client.auction_id].add(client)
        self._per_ip[client.ip] += 1

    def join_admin(self, client: Client) -> None:
        self._admins.add(client)
        self._per_ip[client.ip] += 1

    def leave(self, client: Client) -> None:
        if client in self._admins:
            self._admins.discard(client)
        else:
            room = self._rooms.get(client.auction_id)
            if room is None or client not in room:
                return
            room.discard(client)
            if not room:
                del self._rooms[client.auction_id]
        self._per_ip[client.ip] -= 1
        if self._per_ip[client.ip] <= 0:
            del self._per_ip[client.ip]

    def connections_from(self, ip: str) -> int:
        return self._per_ip.get(ip, 0)

    def publish(self, auction_id: int, text: str) -> None:
        for client in list(self._rooms.get(auction_id, ())):
            client.offer(text)
        for client in list(self._admins):
            client.offer(text)

    def publish_admins(self, text: str) -> None:
        for client in list(self._admins):
            client.offer(text)

    def auction_ids(self) -> list[int]:
        return list(self._rooms)

    def client_count(self) -> int:
        return sum(len(room) for room in self._rooms.values())

    def admin_count(self) -> int:
        return len(self._admins)

    def close_all(self, code: int = SERVER_RESTART, reason: str = "server restarting") -> None:
        for room in list(self._rooms.values()):
            for client in list(room):
                client.close(code, reason)
        for client in list(self._admins):
            client.close(code, reason)
