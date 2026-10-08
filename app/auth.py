"""Bidder accounts: email + password, sessions in an HttpOnly cookie.

- Passwords are hashed with scrypt (salted, memory-hard) and never stored.
- A session is a random 256-bit token in a cookie the page's JavaScript can't
  read (HttpOnly), that browsers won't send on cross-site requests
  (SameSite=Lax), and that only travels over HTTPS (Secure, on the public
  server). The database keeps only its SHA-256, so a leaked sessions table
  can't be used to log in.
- Requests that change something, and WebSocket handshakes, must come from
  this site (Origin check), so another website can't act with a visitor's
  cookie - that covers CSRF and cross-site WebSocket hijacking.
- Wrong passwords are throttled per address, and a failed lookup for an
  unknown email still runs scrypt, so timing doesn't reveal which emails exist.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import asyncpg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.security import client_ip

COOKIE = "bf_session"
SESSION_DAYS = 14
# scrypt cost: 16 MiB and ~30 ms per hash - expensive to brute-force offline,
# cheap enough that a t3.micro can still log people in.
SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2**14, 8, 1, 32

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,23}$")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return scheme == "scrypt" and hmac.compare_digest(actual, expected)


# Checked against when an email doesn't exist, so a wrong email takes as long as
# a wrong password and response times don't reveal who has an account.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class User:
    id: int
    email: str
    display_name: str

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "email": self.email, "display_name": self.display_name}


async def user_for_token(pool: asyncpg.Pool, token: str | None) -> User | None:
    if not token or len(token) > 128:
        return None
    row = await pool.fetchrow(
        """
        SELECT u.id, u.email, u.display_name FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = $1 AND s.expires_at > now()
        """,
        token_hash(token),
    )
    return User(row["id"], row["email"], row["display_name"]) if row else None


async def current_user(request: Request) -> User | None:
    return await user_for_token(request.app.state.pool, request.cookies.get(COOKIE))


def same_origin(headers: Any) -> bool:
    """A browser always sends Origin on cross-site requests and WebSocket
    handshakes. No Origin means a non-browser client (curl, a script), which
    can't be riding a visitor's cookie."""
    origin = headers.get("origin")
    if not origin:
        return True
    return urlsplit(origin).netloc == headers.get("host")


def require_same_origin(request: Request) -> None:
    if not same_origin(request.headers):
        raise HTTPException(403, "requests must come from this site")


class SignupIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=3, max_length=24)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


router = APIRouter(prefix="/auth", tags=["accounts"])


def _ip(request: Request) -> str:
    peer = request.client.host if request.client else None
    return client_ip(request.scope.get("headers", []), peer, request.app.state.settings.trusted_proxy_hops)


async def _start_session(pool: asyncpg.Pool, response: Response, user: User, secure: bool) -> None:
    token = secrets.token_urlsafe(32)
    await pool.execute("DELETE FROM sessions WHERE expires_at < now()")
    await pool.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES ($1, $2, now() + make_interval(days => $3))",
        token_hash(token), user.id, SESSION_DAYS,
    )
    response.set_cookie(COOKIE, token, max_age=SESSION_DAYS * 86400, path="/",
                        httponly=True, samesite="lax", secure=secure)


@router.post("/signup", status_code=201)
async def signup(body: SignupIn, request: Request, response: Response) -> dict[str, Any]:
    require_same_origin(request)
    state = request.app.state
    if not state.signup_limiter.allow(_ip(request)):
        raise HTTPException(429, "too many new accounts from your address; try again later")
    email, name = body.email.strip(), body.display_name.strip()
    if not EMAIL_RE.match(email):
        raise HTTPException(400, "That doesn't look like an email address.")
    if not NAME_RE.match(name):
        raise HTTPException(400, "Display names are 3-24 letters, numbers, dots, dashes or underscores.")
    if body.password.strip().lower() == email.lower():
        raise HTTPException(400, "Your password can't be your email address.")
    password_hash = await asyncio.to_thread(hash_password, body.password)
    try:
        row = await state.pool.fetchrow(
            "INSERT INTO users (email, display_name, password_hash) VALUES ($1, $2, $3)"
            " RETURNING id, email, display_name",
            email, name, password_hash,
        )
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name == "users_email_unique":
            raise HTTPException(409, "An account with this email already exists. Log in instead.") from None
        raise HTTPException(409, "That display name is taken. Pick another.") from None
    user = User(row["id"], row["email"], row["display_name"])
    await _start_session(state.pool, response, user, state.settings.cookie_secure)
    return {"user": user.public()}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    require_same_origin(request)
    state = request.app.state
    ip = _ip(request)
    if not state.login_failures.peek(ip):
        raise HTTPException(429, "Too many wrong passwords from your address. Wait a minute and try again.")
    row = await state.pool.fetchrow(
        "SELECT id, email, display_name, password_hash FROM users WHERE lower(email) = lower($1)",
        body.email.strip(),
    )
    ok = await asyncio.to_thread(verify_password, body.password, row["password_hash"] if row else _DUMMY_HASH)
    if row is None or not ok:
        state.login_failures.allow(ip)
        raise HTTPException(401, "Email or password is incorrect.")
    await state.pool.execute("UPDATE users SET last_login_at = now() WHERE id = $1", row["id"])
    user = User(row["id"], row["email"], row["display_name"])
    await _start_session(state.pool, response, user, state.settings.cookie_secure)
    return {"user": user.public()}


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    require_same_origin(request)
    token = request.cookies.get(COOKIE)
    if token:
        await request.app.state.pool.execute("DELETE FROM sessions WHERE token_hash = $1", token_hash(token))
    response.delete_cookie(COOKIE, path="/")


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    user = await current_user(request)
    return {"user": user.public() if user else None, "require_login": request.app.state.settings.require_login}
