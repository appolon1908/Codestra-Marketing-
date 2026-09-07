#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
REVISIONS = ("001_stage4", "002_stage5", "003_operations", "004_provider_boundary")
UP = [ROOT / f"migrations/{revision}.sql" for revision in REVISIONS]
DOWN = [ROOT / f"migrations/{revision}.down.sql" for revision in reversed(REVISIONS)]
TABLES = (
    "campaigns", "campaign_approvals", "audiences", "creatives",
    "marketing_operations", "marketing_outbox", "marketing_audit_events",
    "marketing_attribution_touches", "marketing_provider_campaigns", "marketing_provider_commands",
)


def dsn() -> str:
    value = os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL", "")
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


async def execute_files(conn: asyncpg.Connection, paths: list[Path]) -> None:
    for path in paths:
        await conn.execute(path.read_text(encoding="utf-8"))


async def assert_present(conn: asyncpg.Connection) -> None:
    for table in TABLES:
        assert await conn.fetchval("SELECT to_regclass($1)", f"public.{table}") == table
    for index in ("uq_campaign_idempotency", "uq_marketing_operation_idempotency", "uq_provider_command_key"):
        assert await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND indexname=$1", index
        ) == 1


async def assert_absent(conn: asyncpg.Connection) -> None:
    for table in TABLES:
        assert await conn.fetchval("SELECT to_regclass($1)", f"public.{table}") is None


async def main() -> None:
    if not dsn():
        raise SystemExit("POSTGRES_DSN or DATABASE_URL is required")
    conn = await asyncpg.connect(dsn())
    try:
        # Disposable certification database ONLY; never run against production.
        if await conn.fetchval("SELECT to_regclass('public.campaigns')") is not None:
            await execute_files(conn, DOWN)
        await assert_absent(conn)
        await execute_files(conn, UP)
        await assert_present(conn)
        await execute_files(conn, DOWN)
        await assert_absent(conn)
        await execute_files(conn, UP)
        await assert_present(conn)
    finally:
        await conn.close()
    print("MARKETING_STAGE5_POSTGRES_CERTIFICATION=PASS")


if __name__ == "__main__":
    asyncio.run(main())
