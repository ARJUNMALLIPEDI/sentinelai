import pytest
from pydantic import ValidationError

from tests.conftest import make_settings


def test_plain_postgres_url_is_normalised_to_asyncpg() -> None:
    s = make_settings(database_url="postgresql://u:p@db:5432/x")
    assert s.database_url == "postgresql+asyncpg://u:p@db:5432/x"


def test_heroku_style_postgres_scheme_is_accepted() -> None:
    s = make_settings(database_url="postgres://u:p@db/x")
    assert s.database_url.startswith("postgresql+asyncpg://")


@pytest.mark.parametrize(
    "url", ["mysql://u:p@db/x", "sqlite:///x.db", "postgresql+psycopg2://u@db/x"]
)
def test_non_asyncpg_database_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        make_settings(database_url=url)


def test_redis_url_requires_redis_scheme() -> None:
    with pytest.raises(ValidationError, match="REDIS_URL"):
        make_settings(redis_url="http://localhost:6379")


def test_api_key_is_never_rendered() -> None:
    s = make_settings(anthropic_api_key="sk-ant-secret-value")
    assert "sk-ant-secret-value" not in repr(s)
    assert s.anthropic_api_key is not None
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-secret-value"


def test_cors_origins_parse_from_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
    monkeypatch.setenv("REDIS_URL", "redis://r:6379/0")
    monkeypatch.setenv("CORS_ORIGINS", '["http://a.test", "http://b.test"]')
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.cors_origins == ["http://a.test", "http://b.test"]
