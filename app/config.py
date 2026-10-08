"""Runtime settings, read from the environment (and an optional .env file)."""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Real environment variables win over .env, so containers and CI can override it.
load_dotenv(ROOT / ".env")

# Largest amount the API accepts, well inside BIGINT.
MAX_AMOUNT = 1_000_000_000_000

# An admin key shorter than this is treated as unset (admin portal stays off).
MIN_ADMIN_KEY_LENGTH = 24


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    pool_min: int = 2
    pool_max: int = 20
    closer_interval: float = 1.0
    listener_healthcheck: float = 5.0
    client_queue_max: int = 256
    enable_unsafe_demo: bool = False
    unsafe_delay_ms: int = 20
    cors_origins: tuple[str, ...] = ()
    demo_restock: bool = False
    demo_open_lots: int = 4
    # Admin portal: off unless a long enough key is configured.
    admin_key: str | None = None
    # Whether anyone may open a lot (local demo) or only the admin (public server).
    public_lot_creation: bool = True
    # How many proxies in front of us append to X-Forwarded-For (CloudFront + nginx = 2).
    # 0 = use the TCP peer address and ignore the header entirely.
    trusted_proxy_hops: int = 0
    rate_limits: bool = True
    bid_rate_per_sec: float = 30.0
    bid_burst: int = 400
    ws_connections_per_ip: int = 50
    max_body_bytes: int = 16 * 1024
    instance_name: str = "local"
    # A shareable judge key (full admin control, shown on the landing page).
    judge_access: bool = False
    judge_open_lot_cap: int = 12
    judge_writes_per_min: int = 30
    # Bidding needs an account; the bidder name comes from it, never the client.
    require_login: bool = False
    # Set the session cookie's Secure flag (true behind HTTPS).
    cookie_secure: bool = False
    signups_per_hour: int = 20
    # Anti-sniping: a bid this close to the end pushes the end back this far. 0 = off.
    soft_close_seconds: int = 30


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/bidding"
        ),
        pool_min=_int("DB_POOL_MIN", 2),
        pool_max=_int("DB_POOL_MAX", 20),
        closer_interval=_float("CLOSER_INTERVAL_SECONDS", 1.0),
        listener_healthcheck=_float("LISTENER_HEALTHCHECK_SECONDS", 5.0),
        client_queue_max=_int("CLIENT_QUEUE_MAX", 256),
        enable_unsafe_demo=_bool("ENABLE_UNSAFE_DEMO", False),
        unsafe_delay_ms=_int("UNSAFE_DELAY_MS", 20),
        cors_origins=tuple(
            o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
        ),
        demo_restock=_bool("DEMO_RESTOCK", False),
        demo_open_lots=_int("DEMO_OPEN_LOTS", 4),
        admin_key=_admin_key(),
        public_lot_creation=_bool("PUBLIC_LOT_CREATION", True),
        trusted_proxy_hops=_int("TRUSTED_PROXY_HOPS", 0),
        rate_limits=_bool("RATE_LIMITS", True),
        bid_rate_per_sec=_float("BID_RATE_PER_SEC", 30.0),
        bid_burst=_int("BID_BURST", 400),
        ws_connections_per_ip=_int("WS_CONNECTIONS_PER_IP", 50),
        max_body_bytes=_int("MAX_BODY_BYTES", 16 * 1024),
        instance_name=os.environ.get("INSTANCE_NAME") or socket.gethostname(),
        judge_access=_bool("JUDGE_ACCESS", False),
        judge_open_lot_cap=_int("JUDGE_OPEN_LOT_CAP", 12),
        judge_writes_per_min=_int("JUDGE_WRITES_PER_MIN", 30),
        require_login=_bool("REQUIRE_LOGIN", False),
        cookie_secure=_bool("COOKIE_SECURE", False),
        signups_per_hour=_int("SIGNUPS_PER_HOUR", 20),
        soft_close_seconds=_int("SOFT_CLOSE_SECONDS", 30),
    )


def _admin_key() -> str | None:
    key = os.environ.get("ADMIN_KEY", "").strip()
    if not key:
        return None
    if len(key) < MIN_ADMIN_KEY_LENGTH:
        logging.getLogger(__name__).warning(
            "ADMIN_KEY is shorter than %d characters; the admin portal stays off", MIN_ADMIN_KEY_LENGTH
        )
        return None
    return key
