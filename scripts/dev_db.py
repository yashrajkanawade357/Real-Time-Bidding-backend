"""A throwaway local Postgres, for machines without Docker or a Postgres install.

    npm install --prefix .devdb @embedded-postgres/windows-x64   # or darwin-arm64, linux-x64, ...
    python scripts/dev_db.py start     # init on first run, start, create the databases
    python scripts/dev_db.py stop
    python scripts/dev_db.py status

Data lives in .devdb/data (gitignored). Trust auth, localhost only - dev use only.
Set PG_BIN to use Postgres binaries from somewhere else.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEVDB = ROOT / ".devdb"
DATA = DEVDB / "data"
LOG = DEVDB / "postgres.log"
PORT = int(os.environ.get("DEV_DB_PORT", "54329"))
DATABASES = ("bidding", "bidding_test")


def pg_bin() -> Path:
    if os.environ.get("PG_BIN"):
        return Path(os.environ["PG_BIN"])
    found = sorted((DEVDB / "node_modules" / "@embedded-postgres").glob("*/native/bin"))
    if not found:
        sys.exit("No Postgres binaries. Run: npm install --prefix .devdb @embedded-postgres/<platform>")
    return found[0]


def tool(name: str) -> str:
    exe = pg_bin() / (name + (".exe" if os.name == "nt" else ""))
    return str(exe)


def run(*args: str) -> int:
    # pg_ctl's child server inherits our handles; detach them so we don't hang on it.
    return subprocess.call(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)


async def create_databases() -> None:
    import asyncpg

    conn = await asyncpg.connect(f"postgresql://postgres@localhost:{PORT}/postgres")
    try:
        for name in DATABASES:
            if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", name):
                await conn.execute(f'CREATE DATABASE "{name}"')
                print(f"created database {name}")
    finally:
        await conn.close()


def start() -> None:
    if not (DATA / "PG_VERSION").exists():
        DEVDB.mkdir(exist_ok=True)
        subprocess.check_call([tool("initdb"), "-D", str(DATA), "-U", "postgres",
                               "--auth=trust", "-E", "UTF8", "--locale=C"],
                              stdout=subprocess.DEVNULL)
    if run(tool("pg_ctl"), "-D", str(DATA), "status") == 0:
        print("already running")
    else:
        code = run(tool("pg_ctl"), "-D", str(DATA), "-l", str(LOG), "-w", "-o",
                   f"-p {PORT} -c listen_addresses=localhost -c max_connections=200", "start")
        if code != 0:
            sys.exit(f"Postgres failed to start; see {LOG}")
    asyncio.run(create_databases())
    print(f"Postgres on localhost:{PORT}")
    print(f"  DATABASE_URL=postgresql://postgres@localhost:{PORT}/bidding")
    print(f"  TEST_DATABASE_URL=postgresql://postgres@localhost:{PORT}/bidding_test")


def stop() -> None:
    run(tool("pg_ctl"), "-D", str(DATA), "-m", "fast", "stop")
    print("stopped")


def status() -> None:
    running = run(tool("pg_ctl"), "-D", str(DATA), "status") == 0
    print(f"{'running' if running else 'stopped'} (port {PORT}, data {DATA})")


if __name__ == "__main__":
    commands = {"start": start, "stop": stop, "status": status}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(__doc__)
    commands[sys.argv[1]]()
