"""The shareable judge key: published, powerful, limited, and under the owner's control."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from tests.conftest import recv_until, running_server

OWNER_KEY = "owner-key-0123456789abcdefghijkl"
LOT = {"title": "Judge lot", "starting_price": 100, "min_increment": 10, "duration_seconds": 600}


@pytest.fixture
def judge_settings(settings):
    return replace(settings, admin_key=OWNER_KEY, judge_access=True, public_lot_creation=False,
                   judge_open_lot_cap=3, judge_writes_per_min=60)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_judge_key_is_off_unless_enabled(pool, settings):
    async with running_server(replace(settings, admin_key=OWNER_KEY)) as addr, \
            httpx.AsyncClient(base_url=f"http://{addr}") as http:
        assert (await http.get("/config")).json()["judge_key"] is None


async def test_judge_key_is_published_and_works(pool, judge_settings):
    async with running_server(judge_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        key = (await http.get("/config")).json()["judge_key"]
        assert key and len(key) >= 32
        overview = (await http.get("/admin/api/overview", headers=bearer(key))).json()
        assert overview["you"] == "judge"
        assert overview["judge_access"] == {"enabled": True, "key": None, "since": overview["judge_access"]["since"]}

        owner_view = (await http.get("/admin/api/overview", headers=bearer(OWNER_KEY))).json()
        assert owner_view["you"] == "owner" and owner_view["judge_access"]["key"] == key

        lot = (await http.post("/admin/api/lots", headers=bearer(key), json=LOT)).json()
        assert (await http.post(f"/admin/api/lots/{lot['id']}/close", headers=bearer(key))).status_code == 200
        assert (await http.post(f"/admin/api/lots/{lot['id']}/remove", headers=bearer(key))).status_code == 200


async def test_judges_cannot_touch_keys(pool, judge_settings):
    async with running_server(judge_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        key = (await http.get("/config")).json()["judge_key"]
        for path in ("/admin/api/judge-key/rotate", "/admin/api/judge-key/disable"):
            assert (await http.post(path, headers=bearer(key))).status_code == 403
        activity = (await http.get("/admin/api/activity", headers=bearer(key))).json()
    assert activity == []  # refused requests change nothing and log nothing


async def test_judges_can_only_keep_a_few_lots_open(pool, judge_settings):
    async with running_server(judge_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        key = (await http.get("/config")).json()["judge_key"]
        codes = [(await http.post("/admin/api/lots", headers=bearer(key), json=LOT)).status_code for _ in range(4)]
        owner = await http.post("/admin/api/lots", headers=bearer(OWNER_KEY), json=LOT)
    assert codes == [201, 201, 201, 409]
    assert owner.status_code == 201  # the cap is for the shared key only


async def test_judge_changes_are_rate_limited(pool, judge_settings):
    limited = replace(judge_settings, judge_writes_per_min=2, judge_open_lot_cap=50)
    async with running_server(limited) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        key = (await http.get("/config")).json()["judge_key"]
        codes = [(await http.post("/admin/api/lots", headers=bearer(key), json=LOT)).status_code for _ in range(3)]
    assert codes == [201, 201, 429]


async def test_owner_rotates_and_disables_the_judge_key(pool, judge_settings):
    async with running_server(judge_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        old = (await http.get("/config")).json()["judge_key"]
        rotated = (await http.post("/admin/api/judge-key/rotate", headers=bearer(OWNER_KEY))).json()
        new = rotated["key"]
        assert new != old
        assert (await http.get("/config")).json()["judge_key"] == new
        assert (await http.get("/admin/api/lots", headers=bearer(old))).status_code == 401
        assert (await http.get("/admin/api/lots", headers=bearer(new))).status_code == 200

        await http.post("/admin/api/judge-key/disable", headers=bearer(OWNER_KEY))
        assert (await http.get("/config")).json()["judge_key"] is None
        assert (await http.get("/admin/api/lots", headers=bearer(new))).status_code == 401
        assert (await http.get("/admin/api/lots", headers=bearer(OWNER_KEY))).status_code == 200

        await http.post("/admin/api/judge-key/enable", headers=bearer(OWNER_KEY))
        assert (await http.get("/config")).json()["judge_key"] == new


async def test_every_change_is_attributed(pool, judge_settings):
    async with running_server(judge_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        key = (await http.get("/config")).json()["judge_key"]
        lot = (await http.post("/admin/api/lots", headers=bearer(key), json=LOT)).json()
        await http.post(f"/admin/api/lots/{lot['id']}/close", headers=bearer(OWNER_KEY))
        await http.post("/admin/api/judge-key/rotate", headers=bearer(OWNER_KEY))
        as_owner = (await http.get("/admin/api/activity", headers=bearer(OWNER_KEY))).json()
        new_key = (await http.get("/config")).json()["judge_key"]
        as_judge = (await http.get("/admin/api/activity", headers=bearer(new_key))).json()
    assert [(a["role"], a["action"]) for a in as_owner] == [
        ("owner", "rotate judge key"), ("owner", "close lot"), ("judge", "open lot")]
    assert all(a["ip"] for a in as_owner)
    assert all(a["ip"] is None for a in as_judge)  # addresses are for the owner's eyes


async def test_admin_feed_accepts_the_judge_key(pool, judge_settings):
    async with running_server(judge_settings) as addr:
        async with httpx.AsyncClient(base_url=f"http://{addr}") as http:
            key = (await http.get("/config")).json()["judge_key"]
        async with connect(f"ws://{addr}/ws/admin") as feed:
            await feed.send(json.dumps({"type": "auth", "key": key}))
            hello = await recv_until(feed, "hello")
        assert hello["role"] == "judge"
        async with connect(f"ws://{addr}/ws/admin") as feed:
            await feed.send(json.dumps({"type": "auth", "key": "x" * 32}))
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(feed.recv(), 5)
        assert closed.value.rcvd.code == 4401
