"""Migrations are tested like code: they must apply cleanly, roll back cleanly,
and match the SQLAlchemy models exactly (``alembic check`` == no drift)."""

import asyncio

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import TEST_DATABASE_URL, alembic_config

pytestmark = pytest.mark.integration


async def _scalar(sql: str) -> object:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).scalar()
    finally:
        await engine.dispose()


async def _insert_service(
    tier: str = "standard", instances: int = 1, kind: str = "api", slo_error: float = 0.01
) -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO services (id, name, kind, tier, desired_instances, "
                    "slo_latency_p95_ms, slo_error_rate) "
                    "VALUES (gen_random_uuid(), :n, :k, :t, :i, 300, :e)"
                ),
                {
                    "n": f"svc-{tier}-{instances}-{kind}-{slo_error}",
                    "k": kind,
                    "t": tier,
                    "i": instances,
                    "e": slo_error,
                },
            )
    finally:
        await engine.dispose()


def test_migrations_apply_match_models_and_roll_back(migrated_db: None) -> None:
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")

        assert asyncio.run(_scalar("SELECT to_regclass('public.metric_samples') IS NOT NULL"))
        command.check(cfg)  # raises if models and migrations have drifted

        # The CHECK constraints actually exist and are enforced by Postgres.
        asyncio.run(_insert_service("critical", 2))
        for bad in (
            {"tier": "bogus-tier"},
            {"instances": -1},
            {"kind": "mainframe"},
            {"slo_error": 1.5},
        ):
            with pytest.raises(IntegrityError):
                asyncio.run(_insert_service(**bad))  # type: ignore[arg-type]

        command.downgrade(cfg, "base")
        assert asyncio.run(_scalar("SELECT to_regclass('public.services') IS NULL")) is True
    finally:
        # Leave the schema at head for the rest of the session.
        command.upgrade(cfg, "head")
