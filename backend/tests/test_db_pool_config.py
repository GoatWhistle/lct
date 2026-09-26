from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_database_pool_defaults_fit_two_backend_node_budget():
    settings = Settings(_env_file=None)
    assert settings.db_pool_size == 20
    assert settings.db_pool_max_overflow == 10
    # Two backend nodes use at most 60 application connections, leaving room
    # under PostgreSQL's common 100-connection default for admin/backup work.
    assert 2 * (settings.db_pool_size + settings.db_pool_max_overflow) == 60


@pytest.mark.parametrize("values", [{"db_pool_size": 0}, {"db_pool_max_overflow": -1}])
def test_database_pool_rejects_invalid_limits(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_engine_uses_configured_pool_limits(monkeypatch):
    from app.db import base

    observed = {}
    sentinel = object()

    def capture(url, **options):
        observed["url"] = url
        observed.update(options)
        return sentinel

    settings = SimpleNamespace(
        database_url="postgresql+asyncpg://lct:test@localhost/lct",
        db_pool_size=12,
        db_pool_max_overflow=7,
    )
    monkeypatch.setattr(base, "get_settings", lambda: settings)
    monkeypatch.setattr(base, "create_async_engine", capture)
    base.reset()
    try:
        assert base.get_engine() is sentinel
    finally:
        base.reset()

    assert observed == {
        "url": settings.database_url,
        "pool_pre_ping": True,
        "pool_size": 12,
        "max_overflow": 7,
        "hide_parameters": True,
    }
