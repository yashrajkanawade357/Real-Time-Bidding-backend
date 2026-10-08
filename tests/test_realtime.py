"""End to end over real sockets: live pushes, reconnects, restarts, multiple instances."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app import auctions, bidding
from tests.conftest import (
    assert_consistent,
    recv_all,
    recv_until,
    running_server,
    wait_for_listener,
)


async def _auction(pool, **kw):
    return await auctions.create(
        pool, title=kw.get("title", "Live lot"), description="",
        starting_price=kw.get("start", 100), min_increment=kw.get("inc", 10),
        duration_seconds=kw.get("seconds", 300),
    )


def _ws(addr: str, auction_id: int, bidder: str | None = None):
    query = f"?bidder={bidder}" if bidder else ""
    return connect(f"ws://{addr}/ws/auctions/{auction_id}{query}")


async def test_connect_receives_snapshot_from_database(pool, settings):
    a = await _auction(pool)
    await bidding.place_bid(pool, a["id"], "alice", 100, "r1")
    async with running_server(settings) as addr, _ws(addr, a["id"]) as ws:
        snap = await recv_until(ws, "snapshot")
    assert snap["auction"]["current_price"] == 100
    assert snap["auction"]["version"] == 1
    assert [b["bidder"] for b in snap["bids"]] == ["alice"]


async def test_bid_is_pushed_to_every_connected_client(pool, settings):
    a = await _auction(pool)
    async with running_server(settings) as addr:
        await wait_for_listener(addr)
        async with _ws(addr, a["id"], "alice") as alice, _ws(addr, a["id"], "bob") as bob, \
                _ws(addr, a["id"]) as watcher:
            for ws in (alice, bob, watcher):
                await recv_until(ws, "snapshot")

            await alice.send(json.dumps({"type": "bid", "amount": 120, "request_id": "a-1"}))

            mine = await recv_all(alice, "bid_result", "bid_accepted")
            assert mine["bid_result"]["accepted"] is True
            assert mine["bid_result"]["request_id"] == "a-1"
            for event in (mine["bid_accepted"], await recv_until(bob, "bid_accepted"),
                          await recv_until(watcher, "bid_accepted")):
                assert event["auction"]["current_price"] == 120
                assert event["auction"]["leader"] == "alice"
                assert event["auction"]["version"] == 1


async def test_lower_bid_over_websocket_is_rejected_and_not_broadcast(pool, settings):
    a = await _auction(pool)
    await bidding.place_bid(pool, a["id"], "alice", 300, "r1")
    async with running_server(settings) as addr, _ws(addr, a["id"], "bob") as bob:
        await recv_until(bob, "snapshot")
        await bob.send(json.dumps({"type": "bid", "amount": 250, "request_id": "b-1"}))
        result = await recv_until(bob, "bid_result", request_id="b-1")
        assert (result["accepted"], result["reason"]) == (False, "bid_too_low")
        assert result["auction"]["min_next_bid"] == 310
        # Nothing else should arrive: a rejected bid changes no state.
        await bob.send('{"type": "ping"}')
        assert json.loads(await bob.recv())["type"] == "pong"


async def test_reconnecting_client_sees_what_happened_while_it_was_away(pool, settings):
    a = await _auction(pool)
    async with running_server(settings) as addr:
        async with _ws(addr, a["id"], "alice") as ws:
            assert (await recv_until(ws, "snapshot"))["auction"]["version"] == 0
        # alice is offline; three bids land
        async with httpx.AsyncClient(base_url=f"http://{addr}") as http:
            for bidder, amount in [("bob", 100), ("carol", 150), ("bob", 200)]:
                r = await http.post(f"/auctions/{a['id']}/bids", json={"bidder": bidder, "amount": amount})
                assert r.status_code == 201
        async with _ws(addr, a["id"], "alice") as ws:
            snap = await recv_until(ws, "snapshot")
    assert snap["auction"]["version"] == 3
    assert (snap["auction"]["current_price"], snap["auction"]["leader"]) == (200, "bob")
    assert [b["amount"] for b in snap["bids"]] == [200, 150, 100]


async def test_bid_resent_after_dropped_connection_is_not_doubled(pool, settings):
    """The client sends a bid and the connection dies before it hears back.
    It can't know whether the bid landed, so it resends with the same
    request_id. The bid must exist exactly once."""
    a = await _auction(pool)
    async with running_server(settings) as addr:
        async with _ws(addr, a["id"], "alice") as ws:
            await recv_until(ws, "snapshot")
            await ws.send(json.dumps({"type": "bid", "amount": 500, "request_id": "flaky-1"}))
        # connection closed without reading the result

        async with _ws(addr, a["id"], "alice") as ws:
            await recv_until(ws, "snapshot")
            await ws.send(json.dumps({"type": "bid", "amount": 500, "request_id": "flaky-1"}))
            result = await recv_until(ws, "bid_result", request_id="flaky-1")
    assert result["accepted"] is True
    assert result["auction"]["bid_count"] == 1
    assert await pool.fetchval("SELECT count(*) FROM bids WHERE request_id = 'flaky-1'") == 1
    await assert_consistent(pool, a["id"])


async def test_state_survives_a_server_restart(pool, settings):
    """Nothing lives only in memory: a brand-new server process serves the same state."""
    a = await _auction(pool)
    async with running_server(settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        for bidder, amount in [("alice", 100), ("bob", 110), ("alice", 130)]:
            await http.post(f"/auctions/{a['id']}/bids", json={"bidder": bidder, "amount": amount})
    # first server is gone; start a fresh one
    async with running_server(settings) as addr, _ws(addr, a["id"], "carol") as ws:
        snap = await recv_until(ws, "snapshot")
    assert (snap["auction"]["current_price"], snap["auction"]["leader"]) == (130, "alice")
    assert snap["auction"]["version"] == 3


async def test_server_shutdown_tells_clients_to_reconnect(pool, settings):
    a = await _auction(pool)
    async with running_server(settings) as addr:
        ws = await _ws(addr, a["id"], "alice")
        await recv_until(ws, "snapshot")
    # server stopped; skip anything still buffered (e.g. a resync snapshot)
    try:
        async with asyncio.timeout(5):
            while True:
                await ws.recv()
    except ConnectionClosed as closed:
        assert closed.rcvd is not None and closed.rcvd.code in (1001, 1012)


async def test_two_instances_share_live_updates(pool, settings):
    """A client on instance B hears about a bid placed through instance A.
    No sticky sessions: Postgres LISTEN/NOTIFY fans the event out."""
    a = await _auction(pool)
    async with running_server(settings) as addr_a, running_server(settings) as addr_b:
        await wait_for_listener(addr_b)
        async with _ws(addr_b, a["id"], "bob") as bob:
            await recv_until(bob, "snapshot")
            async with httpx.AsyncClient(base_url=f"http://{addr_a}") as http:
                r = await http.post(f"/auctions/{a['id']}/bids", json={"bidder": "alice", "amount": 140})
                assert r.status_code == 201
            event = await recv_until(bob, "bid_accepted")
    assert (event["auction"]["current_price"], event["auction"]["leader"]) == (140, "alice")


async def test_lost_event_feed_is_followed_by_a_fresh_snapshot(pool, settings):
    """If the server's own LISTEN connection to Postgres drops, events during
    the gap are gone - so after reconnecting it re-sends every client a snapshot."""
    a = await _auction(pool)
    async with running_server(settings) as addr:
        await wait_for_listener(addr)
        async with _ws(addr, a["id"], "alice") as ws:
            await recv_until(ws, "snapshot")
            killed = await pool.fetchval(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                "WHERE application_name = 'bidding-listener' AND datname = current_database()"
            )
            assert killed == 1
            await bidding.place_bid(pool, a["id"], "bob", 100, "during-gap")
            # Whether or not that event slipped through, a resync snapshot follows.
            snap = await recv_until(ws, "snapshot", timeout=10)
            assert snap["auction"]["version"] >= 1
            # and the feed works again afterwards
            await wait_for_listener(addr)
            await bidding.place_bid(pool, a["id"], "carol", 200, "after-gap")
            event = await recv_until(ws, "bid_accepted", timeout=5)
            while event["auction"]["version"] < 2:
                event = await recv_until(ws, "bid_accepted", timeout=5)
            assert event["auction"]["leader"] == "carol"


async def test_closing_is_broadcast_with_the_winner(pool, settings):
    a = await _auction(pool)
    await bidding.place_bid(pool, a["id"], "alice", 400, "r1")
    async with running_server(settings) as addr:
        await wait_for_listener(addr)
        async with _ws(addr, a["id"]) as ws:
            await recv_until(ws, "snapshot")
            await pool.execute("UPDATE auctions SET ends_at = now() WHERE id = $1", a["id"])
            closed = await recv_until(ws, "auction_closed", timeout=5)
    assert closed["auction"]["status"] == "closed"
    assert (closed["auction"]["leader"], closed["auction"]["current_price"]) == ("alice", 400)


async def test_watcher_without_a_name_cannot_bid(pool, settings):
    a = await _auction(pool)
    async with running_server(settings) as addr, _ws(addr, a["id"]) as ws:
        await recv_until(ws, "snapshot")
        await ws.send(json.dumps({"type": "bid", "amount": 999, "request_id": "x"}))
        result = await recv_until(ws, "bid_result")
    assert (result["accepted"], result["reason"]) == (False, "watch_only")


async def test_unknown_auction_socket_is_closed(pool, settings):
    async with running_server(settings) as addr, _ws(addr, 424242) as ws:
        try:
            await asyncio.wait_for(ws.recv(), 5)
            raise AssertionError("expected close")
        except ConnectionClosed as closed:
            assert closed.rcvd.code == 4404


async def test_unsafe_endpoint_is_off_unless_enabled(pool, settings):
    a = await _auction(pool)
    async with running_server(settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        r = await http.post(f"/auctions/{a['id']}/bids/unsafe", json={"bidder": "x", "amount": 100})
    assert r.status_code == 404
    async with running_server(replace(settings, enable_unsafe_demo=True)) as addr, \
            httpx.AsyncClient(base_url=f"http://{addr}") as http:
        r = await http.post(f"/auctions/{a['id']}/bids/unsafe", json={"bidder": "x", "amount": 100})
    assert r.status_code == 201
