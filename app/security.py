"""Request-level protections: security headers, body size cap, client IP, rate limits.

The threat model and what each control is for are in docs/SECURITY.md.
"""

from __future__ import annotations

import hmac
import json
import time
from collections.abc import Iterable

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Pages and scripts only ever load from this origin; fonts come from Google
# Fonts. No inline scripts or styles anywhere, so none are allowed.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

# FastAPI's interactive docs load Swagger UI from a CDN with inline scripts,
# so those paths get the other headers but not the CSP.
_NO_CSP_PREFIXES = ("/docs", "/redoc", "/openapi.json")


class SecurityMiddleware:
    """Adds security headers to every HTTP response, tags it with the instance
    that served it, and refuses request bodies over `max_body_bytes`."""

    def __init__(self, app: ASGIApp, *, instance: str, max_body_bytes: int) -> None:
        self.app = app
        self.instance = instance.encode()
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = _header(scope, b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_body_bytes:
            await _plain(send, 413, "request body too large")
            return

        path = scope.get("path", "")
        extra = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
            (b"x-served-by", self.instance),
        ]
        if not path.startswith(_NO_CSP_PREFIXES):
            extra.append((b"content-security-policy", CONTENT_SECURITY_POLICY.encode()))

        received = 0

        async def capped_receive() -> Message:
            # Covers bodies sent without a Content-Length (chunked).
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    # Raised inside the app's body read, so FastAPI answers 413 itself.
                    raise HTTPException(413, "request body too large")
            return message

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, capped_receive, send_with_headers)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


async def _plain(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def client_ip(headers: Iterable[tuple[bytes, bytes]], peer: str | None, trusted_hops: int) -> str:
    """The address of the actual client.

    Each trusted proxy appends the address it received the request from to
    X-Forwarded-For, so with N trusted proxies the client is the N-th entry
    from the right. Entries further left were supplied by the client and can
    be forged, so they're never used. With no trusted proxies the header is
    ignored and the TCP peer is the client.
    """
    if trusted_hops > 0:
        forwarded = None
        for key, value in headers:
            if key == b"x-forwarded-for":
                forwarded = value.decode("latin-1")
        if forwarded:
            hops = [h.strip() for h in forwarded.split(",") if h.strip()]
            if len(hops) >= trusted_hops:
                return hops[-trusted_hops]
    return peer or "unknown"


class RateLimiter:
    """Token bucket per key (usually a client IP): `rate` tokens a second, up
    to `burst`. In-process, so each API instance limits on its own - fine for
    abuse protection, and it needs no extra infrastructure."""

    def __init__(self, rate: float, burst: int, *, enabled: bool = True, max_keys: int = 10_000) -> None:
        self.rate = rate
        self.burst = burst
        self.enabled = enabled
        self.max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str, cost: float = 1.0) -> bool:
        if not self.enabled:
            return True
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        tokens = min(float(self.burst), tokens + (now - last) * self.rate)
        allowed = tokens >= cost
        if allowed:
            tokens -= cost
        if len(self._buckets) >= self.max_keys and key not in self._buckets:
            self._prune(now)
        self._buckets[key] = (tokens, now)
        return allowed

    def peek(self, key: str) -> bool:
        """Would one more request be allowed? Checks without using anything up."""
        if not self.enabled:
            return True
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        return min(float(self.burst), tokens + (now - last) * self.rate) >= 1

    def _prune(self, now: float) -> None:
        # Drop buckets that have refilled completely; they carry no state.
        full_after = self.burst / self.rate if self.rate else 0
        self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < full_after}


def key_matches(supplied: str | None, expected: str | None) -> bool:
    """Constant-time comparison, so response timing leaks nothing about the key."""
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied.encode(), expected.encode())
