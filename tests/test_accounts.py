"""Accounts, sessions, and bidding as yourself only."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app import auctions, auth
from tests.conftest import recv_until, running_server, wait_for_listener

PASSWORD = "correct horse battery"


@pytest.fixture
def login_settings(settings):
    return replace(settings, require_login=True)


async def _signup(http: httpx.AsyncClient, name: str, email: str | None = None) -> httpx.Response:
    return await http.post("/auth/signup", json={
        "email": email or f"{name}@example.com", "password": PASSWORD, "display_name": name})


def test_passwords_are_hashed_with_scrypt_and_salted():
    first, second = auth.hash_password(PASSWORD), auth.hash_password(PASSWORD)
    assert first.startswith("scrypt$") and PASSWORD not in first
    assert first != second  # a fresh salt every time
    assert auth.verify_password(PASSWORD, first)
    assert not auth.verify_password("wrong password", first)
    assert not auth.verify_password(PASSWORD, "garbage")


async def test_signup_login_logout(pool, login_settings):
    async with running_server(login_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        r = await _signup(http, "asha")
        assert r.status_code == 201
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        assert (await http.get("/auth/me")).json()["user"]["display_name"] == "asha"

        assert (await http.post("/auth/logout")).status_code == 204
        assert (await http.get("/auth/me")).json()["user"] is None

        bad = await http.post("/auth/login", json={"email": "asha@example.com", "password": "nope-nope"})
        assert (bad.status_code, bad.json()["detail"]) == (401, "Email or password is incorrect.")
        unknown = await http.post("/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
        assert unknown.json()["detail"] == bad.json()["detail"]  # same answer either way
        ok = await http.post("/auth/login", json={"email": "ASHA@example.com", "password": PASSWORD})
        assert ok.status_code == 200 and ok.json()["user"]["display_name"] == "asha"


async def test_the_database_never_holds_a_usable_password_or_session(pool, login_settings):
    async with running_server(login_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        await _signup(http, "asha")
        token = http.cookies.get(auth.COOKIE)
    row = await pool.fetchrow("SELECT password_hash FROM users")
    assert PASSWORD not in row["password_hash"]
    stored = await pool.fetchval("SELECT token_hash FROM sessions")
    assert stored != token and stored == auth.token_hash(token)


async def test_signup_rules(pool, login_settings):
    async with running_server(login_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        assert (await _signup(http, "asha")).status_code == 201
        http.cookies.clear()
        assert (await _signup(http, "asha2", email="ASHA@EXAMPLE.COM")).status_code == 409   # same email
        assert (await _signup(http, "ASHA", email="other@example.com")).status_code == 409   # same name
        assert (await _signup(http, "x y", email="xy@example.com")).status_code == 400       # bad name
        assert (await _signup(http, "valid", email="not-an-email")).status_code == 400
        short = await http.post("/auth/signup", json={"email": "s@example.com", "password": "short", "display_name": "shorty"})
        assert short.status_code == 422


async def test_wrong_passwords_are_throttled(pool, login_settings):
    async with running_server(login_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        await _signup(http, "asha")
        codes = [(await http.post("/auth/login", json={"email": "asha@example.com", "password": "wrong-one"})).status_code
                 for _ in range(11)]
        right = await http.post("/auth/login", json={"email": "asha@example.com", "password": PASSWORD})
    assert codes[:10] == [401] * 10 and codes[10] == 429
    assert right.status_code == 429  # locked out for now, even with the right password


async def test_bids_need_a_login_and_use_the_accounts_name(pool, login_settings):
    a = await auctions.create(pool, title="Accounts", description="", starting_price=100,
                              min_increment=10, duration_seconds=600)
    async with running_server(login_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        anon = await http.post(f"/auctions/{a['id']}/bids", json={"bidder": "asha", "amount": 100})
        assert anon.status_code == 401
        await _signup(http, "kabir")
        # Claiming to be someone else in the body changes nothing.
        r = await http.post(f"/auctions/{a['id']}/bids", json={"bidder": "asha", "amount": 100})
        assert r.status_code == 201 and r.json()["bid"]["bidder"] == "kabir"
    user_id = await pool.fetchval("SELECT user_id FROM bids")
    assert user_id == await pool.fetchval("SELECT id FROM users WHERE display_name = 'kabir'")


async def test_sockets_bid_as_the_logged_in_account(pool, login_settings):
    a = await auctions.create(pool, title="Sockets", description="", starting_price=100,
                              min_increment=10, duration_seconds=600)
    async with running_server(login_settings) as addr:
        await wait_for_listener(addr)
        async with httpx.AsyncClient(base_url=f"http://{addr}") as http:
            await _signup(http, "meera")
            token = http.cookies.get(auth.COOKIE)
        # No cookie: watching works, bidding doesn't - whatever ?bidder= says.
        async with connect(f"ws://{addr}/ws/auctions/{a['id']}?bidder=meera") as anon:
            await recv_until(anon, "snapshot")
            await anon.send(json.dumps({"type": "bid", "amount": 100, "request_id": "a1"}))
            assert (await recv_until(anon, "bid_result"))["reason"] == "watch_only"
        # With the session cookie, the bid is meera's even if ?bidder= lies.
        async with connect(f"ws://{addr}/ws/auctions/{a['id']}?bidder=someone-else",
                           additional_headers={"Cookie": f"{auth.COOKIE}={token}"}) as ws:
            await recv_until(ws, "snapshot")
            await ws.send(json.dumps({"type": "bid", "amount": 100, "request_id": "m1"}))
            result = await recv_until(ws, "bid_result")
    assert result["accepted"] and result["bid"]["bidder"] == "meera"


async def test_other_websites_are_refused(pool, login_settings):
    a = await auctions.create(pool, title="Origin", description="", starting_price=100,
                              min_increment=10, duration_seconds=600)
    evil = {"Origin": "https://evil.example"}
    async with running_server(login_settings) as addr, httpx.AsyncClient(base_url=f"http://{addr}") as http:
        await _signup(http, "asha")
        r = await http.post(f"/auctions/{a['id']}/bids", json={"amount": 100}, headers=evil)
        assert r.status_code == 403  # CSRF: a forged cross-site POST is refused even with the cookie
        assert (await http.post("/auth/logout", headers=evil)).status_code == 403
        same = await http.post(f"/auctions/{a['id']}/bids", json={"amount": 100},
                               headers={"Origin": f"http://{addr}"})
        assert same.status_code == 201
        async with connect(f"ws://{addr}/ws/auctions/{a['id']}", additional_headers=evil) as ws:
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), 5)
        assert closed.value.rcvd.code == 4403  # cross-site WebSocket hijacking


async def test_signups_are_rate_limited(pool, login_settings):
    async with running_server(replace(login_settings, signups_per_hour=2)) as addr, \
            httpx.AsyncClient(base_url=f"http://{addr}") as http:
        codes = [(await _signup(http, f"user{n}")).status_code for n in range(3)]
    assert codes == [201, 201, 429]
