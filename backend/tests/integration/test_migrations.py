"""Migrations are tested like code: they must apply cleanly, roll back cleanly,
and match the SQLAlchemy models exactly (``alembic check`` == no drift)."""

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    return cfg


async def _scalar(sql: str) -> object:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).scalar()
    finally:
        await engine.dispose()


async def _insert_service(tier: str, instances: int) -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO services (id, name, tier, desired_instances) "
                     "VALUES (gen_random_uuid(), :n, :t, :i)"),
                {"n": f"svc-{tier}-{instances}", "t": tier, "i": instances},
            )
    finally:
        await engine.dispose()


def test_migrations_apply_match_models_and_roll_back() -> None:
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    assert asyncio.run(_scalar("SELECT to_regclass('public.services') IS NOT NULL")) is True
    command.check(cfg)  # raises if models and migrations have drifted

    # The CHECK constraints actually exist and are enforced by Postgres.
    asyncio.run(_insert_service("critical", 2))
    with pytest.raises(IntegrityError):
        asyncio.run(_insert_service("bogus-tier", 1))
    with pytest.raises(IntegrityError):
        asyncio.run(_insert_service("standard", -1))

    command.downgrade(cfg, "base")
    assert asyncio.run(_scalar("SELECT to_regclass('public.services') IS NULL")) is True
