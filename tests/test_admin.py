"""The admin API and the admin live feed, behind the access key."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app import auctions
from app.config import _admin_key
from tests.conftest import recv_until, running_server, wait_for_listener

KEY = "test-admin-key-0123456789abcdef"


@pytest.fixture
def admin_settings(settings):
    return replace(settings, admin_key=KEY, public_lot_creation=False)


def _auth(key: str = KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_admin_is_invisible_without_a_configured_key(pool, settings):
    async with running_server(settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        assert (await http.get("/admin/api/overview", headers=_auth())).status_code == 404
        async with connect(f"ws://{addr}/ws/admin") as ws:
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), 5)
        assert closed.value.rcvd.code == 4404


def test_short_keys_are_refused_at_startup(monkeypatch):
    monkeypatch.setenv("ADMIN_KEY", "short")
    assert _admin_key() is None
    monkeypatch.setenv("ADMIN_KEY", KEY)
    assert _admin_key() == KEY


async def test_wrong_or_missing_key_is_rejected(pool, admin_settings):
    async with running_server(admin_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        assert (await http.get("/admin/api/overview")).status_code == 401
        assert (await http.get("/admin/api/overview", headers=_auth("wrong-" + KEY))).status_code == 401
        assert (await http.get("/admin/api/overview", headers={"Authorization": KEY})).status_code == 401
        ok = await http.get("/admin/api/overview", headers=_auth())
    assert ok.status_code == 200
    assert ok.json()["settings"]["public_lot_creation"] is False


async def test_repeated_wrong_keys_lock_the_address_out(pool, admin_settings):
    async with running_server(admin_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        codes = [(await http.get("/admin/api/lots", headers=_auth("nope-" + KEY))).status_code for _ in range(12)]
        after = await http.get("/admin/api/lots", headers=_auth())
    assert codes[:10] == [401] * 10
    assert codes[10:] == [429, 429]
    assert after.status_code == 429  # even the right key waits out the lockout


async def test_admin_runs_the_lot_lifecycle(pool, admin_settings):
    async with running_server(admin_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        await wait_for_listener(addr)
        created = await http.post("/admin/api/lots", headers=_auth(), json={
            "title": "Admin lot", "starting_price": 100, "min_increment": 10, "duration_seconds": 600})
        assert created.status_code == 201
        lot = created.json()["id"]

        async with connect(f"ws://{addr}/ws/auctions/{lot}?bidder=bob") as bob:
            await recv_until(bob, "snapshot")
            await bob.send(json.dumps({"type": "bid", "amount": 100, "request_id": "b1"}))
            await recv_until(bob, "bid_result")
            await bob.send(json.dumps({"type": "bid", "amount": 105, "request_id": "b2"}))  # too low
            await recv_until(bob, "bid_result")

            closed = await http.post(f"/admin/api/lots/{lot}/close", headers=_auth())
            assert closed.status_code == 200
            event = await recv_until(bob, "auction_closed")
            assert (event["auction"]["status"], event["auction"]["leader"]) == ("closed", "bob")
            assert (await http.post(f"/admin/api/lots/{lot}/close", headers=_auth())).status_code == 409

            removed = await http.post(f"/admin/api/lots/{lot}/remove", headers=_auth())
            assert removed.status_code == 200
            assert (await recv_until(bob, "auction_removed"))["auction"]["removed_at"] is not None

        assert all(l["id"] != lot for l in (await http.get("/auctions")).json())
        assert (await http.get(f"/auctions/{lot}")).status_code == 404
        admin_lots = (await http.get("/admin/api/lots", headers=_auth())).json()
        assert next(l for l in admin_lots if l["id"] == lot)["removed_at"] is not None

        log = (await http.get("/admin/api/bids", headers=_auth())).json()
        rejected = (await http.get("/admin/api/bids", headers=_auth(), params={"rejected_only": True})).json()
        overview = (await http.get("/admin/api/overview", headers=_auth())).json()

    assert [(b["request_id"], b["status"], b["title"]) for b in log[:2]] == [
        ("b2", "rejected", "Admin lot"), ("b1", "accepted", "Admin lot")]
    assert [b["reason"] for b in rejected] == ["bid_too_low"]
    assert overview["lots"]["removed"] == 1
    assert overview["bids"] == {"accepted": 1, "rejected": 1, "last_hour": 2}
    assert overview["rejected_by_reason"] == {"bid_too_low": 1}
    assert any(i["name"] == admin_settings.instance_name and i["alive"] for i in overview["instances"])


async def test_admin_feed_sees_every_lot_and_needs_the_key(pool, admin_settings):
    a = await auctions.create(pool, title="Feed A", description="", starting_price=10,
                              min_increment=1, duration_seconds=300)
    b = await auctions.create(pool, title="Feed B", description="", starting_price=10,
                              min_increment=1, duration_seconds=300)
    async with running_server(admin_settings) as addr:
        await wait_for_listener(addr)
        async with connect(f"ws://{addr}/ws/admin") as bad:
            await bad.send(json.dumps({"type": "auth", "key": "wrong-" + KEY}))
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(bad.recv(), 5)
            assert closed.value.rcvd.code == 4401

        async with connect(f"ws://{addr}/ws/admin") as feed:
            await feed.send(json.dumps({"type": "auth", "key": KEY}))
            hello = await recv_until(feed, "hello")
            assert hello["instance"] == admin_settings.instance_name
            async with httpx.AsyncClient(base_url=f"http://{addr}") as http:
                await http.post(f"/auctions/{a['id']}/bids", json={"bidder": "x", "amount": 10})
                await http.post(f"/auctions/{b['id']}/bids", json={"bidder": "y", "amount": 11})
            first = await recv_until(feed, "bid_accepted")
            second = await recv_until(feed, "bid_accepted")
    assert {first["auction"]["title"], second["auction"]["title"]} == {"Feed A", "Feed B"}
