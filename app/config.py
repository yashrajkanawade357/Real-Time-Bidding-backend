"""Runtime settings, read from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Real environment variables win over .env, so containers and CI can override it.
load_dotenv(ROOT / ".env")

# Largest amount the API accepts, well inside BIGINT.
MAX_AMOUNT = 1_000_000_000_000


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
    cors_origins: tuple[str, ...] = ("*",)
    demo_restock: bool = False
    demo_open_lots: int = 4


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
            o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
        ),
        demo_restock=_bool("DEMO_RESTOCK", False),
        demo_open_lots=_int("DEMO_OPEN_LOTS", 4),
    )
