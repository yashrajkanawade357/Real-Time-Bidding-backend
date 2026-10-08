"""Security controls: headers, body size, client IP, rate limits, data exposure."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app import auctions, bidding
from app.security import RateLimiter, client_ip
from tests.conftest import recv_all, recv_until, running_server, wait_for_listener

XFF = b"x-forwarded-for"


def test_forwarded_header_is_ignored_without_trusted_proxies():
    assert client_ip([(XFF, b"1.2.3.4")], "9.9.9.9", trusted_hops=0) == "9.9.9.9"


def test_client_is_the_nth_address_from_the_right():
    # The client forged the first entry; CloudFront added the viewer, nginx added CloudFront.
    headers = [(XFF, b"6.6.6.6, 203.0.113.7, 130.176.0.1")]
    assert client_ip(headers, "172.18.0.5", trusted_hops=2) == "203.0.113.7"
    assert client_ip(headers, "172.18.0.5", trusted_hops=1) == "130.176.0.1"


def test_short_forwarded_header_falls_back_to_the_peer():
    assert client_ip([(XFF, b"203.0.113.7")], "172.18.0.5", trusted_hops=2) == "172.18.0.5"


def test_rate_limiter_allows_a_burst_then_refuses():
    limiter = RateLimiter(rate=0.001, burst=3)
    assert [limiter.allow("ip") for _ in range(5)] == [True, True, True, False, False]
    assert limiter.allow("other-ip")  # buckets are per key
    assert not limiter.peek("ip")


async def test_pages_carry_security_headers(pool, settings):
    async with running_server(settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        for path in ("/", "/floor", "/admin", "/static/app.js", "/auctions"):
            r = await http.get(path)
            assert r.status_code == 200, path
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
            assert "script-src 'self'" in r.headers["content-security-policy"]
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["referrer-policy"] == "no-referrer"
            assert r.headers["x-served-by"] == settings.instance_name
        # Swagger UI needs its CDN scripts, so the docs page is exempt from the CSP.
        assert "content-security-policy" not in (await http.get("/docs")).headers


async def test_oversized_bodies_are_refused(pool, settings):
    async with running_server(settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        r = await http.post("/auctions", content=b"x" * (settings.max_body_bytes + 1),
                            headers={"content-type": "application/json"})
    assert r.status_code == 413


async def test_bid_flood_from_one_address_is_throttled(pool, settings):
    a = await auctions.create(pool, title="Throttle", description="", starting_price=1,
                              min_increment=1, duration_seconds=300)
    limited = replace(settings, bid_rate_per_sec=0.01, bid_burst=3)
    async with running_server(limited) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        codes = [(await http.post(f"/auctions/{a['id']}/bids", json={"bidder": "x", "amount": n})).status_code
                 for n in range(1, 7)]
    assert codes[:3] == [201, 201, 201]
    assert codes[3:] == [429, 429, 429]


async def test_too_many_sockets_from_one_address_are_refused(pool, settings):
    a = await auctions.create(pool, title="Sockets", description="", starting_price=1,
                              min_increment=1, duration_seconds=300)
    async with running_server(replace(settings, ws_connections_per_ip=2)) as addr:
        url = f"ws://{addr}/ws/auctions/{a['id']}"
        async with connect(url) as one, connect(url) as two:
            await recv_until(one, "snapshot")
            await recv_until(two, "snapshot")
            async with connect(url) as three:
                try:
                    await asyncio.wait_for(three.recv(), 5)
                    raise AssertionError("third socket should have been closed")
                except ConnectionClosed as closed:
                    assert closed.rcvd.code == 1013


async def test_socket_bid_spam_is_rate_limited(pool, settings):
    a = await auctions.create(pool, title="Spam", description="", starting_price=1,
                              min_increment=1, duration_seconds=300)
    async with running_server(settings) as addr, connect(f"ws://{addr}/ws/auctions/{a['id']}?bidder=spam") as ws:
        await recv_until(ws, "snapshot")
        for n in range(1, 41):
            await ws.send(json.dumps({"type": "bid", "amount": n, "request_id": f"s{n}"}))
        reasons = []
        while len(reasons) < 40:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
            if msg["type"] == "bid_result":
                reasons.append(msg["reason"])
    assert reasons.count("rate_limited") >= 15
    assert reasons.count(None) >= 1  # the first ones went through


async def test_request_ids_are_private_to_the_bidder(pool, settings):
    a = await auctions.create(pool, title="Private", description="", starting_price=100,
                              min_increment=10, duration_seconds=300)
    async with running_server(settings) as addr:
        await wait_for_listener(addr)
        async with connect(f"ws://{addr}/ws/auctions/{a['id']}?bidder=alice") as alice, \
                connect(f"ws://{addr}/ws/auctions/{a['id']}") as watcher:
            await recv_until(alice, "snapshot")
            await recv_until(watcher, "snapshot")
            await alice.send(json.dumps({"type": "bid", "amount": 100, "request_id": "secret-1"}))
            mine = await recv_all(alice, "bid_result", "bid_accepted")
            broadcast = await recv_until(watcher, "bid_accepted")
        async with httpx.AsyncClient(base_url=f"http://{addr}") as http:
            snap = (await http.get(f"/auctions/{a['id']}")).json()
            history = (await http.get(f"/auctions/{a['id']}/bids", params={"include_rejected": True})).json()
    assert mine["bid_result"]["bid"]["request_id"] == "secret-1"
    assert "request_id" not in broadcast["bid"]
    assert "request_id" not in mine["bid_accepted"]["bid"]
    assert all("request_id" not in b for b in snap["bids"] + history)


async def test_public_lot_creation_can_be_switched_off(pool, settings):
    async with running_server(replace(settings, public_lot_creation=False)) as addr, \
            httpx.AsyncClient(base_url=f"http://{addr}") as http:
        r = await http.post("/auctions", json={"title": "Nope", "starting_price": 10})
        config = (await http.get("/config")).json()
    assert r.status_code == 403
    assert config == {"public_lot_creation": False, "instance": settings.instance_name}


async def test_snapshot_says_which_instance_the_socket_is_on(pool, settings):
    a = await auctions.create(pool, title="Where", description="", starting_price=1,
                              min_increment=1, duration_seconds=300)
    named = replace(settings, instance_name="instance-7")
    async with running_server(named) as addr, connect(f"ws://{addr}/ws/auctions/{a['id']}") as ws:
        snap = await recv_until(ws, "snapshot")
    assert snap["instance"] == "instance-7"


async def test_removed_lot_refuses_bids(pool):
    a = await auctions.create(pool, title="Gone", description="", starting_price=1,
                              min_increment=1, duration_seconds=300)
    await bidding.remove(pool, a["id"])
    late = await bidding.place_bid(pool, a["id"], "x", 50, "r1")
    assert (late.accepted, late.reason) == (False, bidding.CLOSED)
