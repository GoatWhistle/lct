"""Общая настройка тестов.

Голосовой контур выключен: он грузит модели около десяти секунд, а проверяется
отдельно в `test_voice_pipeline.py`. Вход включён как на стенде, но тесты
логинятся через `dev-token` — так же, как `make lesson` (tasks/lct-23-auth-roles.md).
"""

import copy
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

os.environ.setdefault("DEV_AUTH_BYPASS", "true")

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402


@pytest.fixture
def postgres_access():
    """Skip DB integration cases when the configured PostgreSQL is unreachable.

    `/api/health` is a liveness endpoint and deliberately does not probe the
    database. Use a short TCP check so sandbox/network-denied runs are reported
    as unverified integration tests instead of misleading application failures.
    """
    url = urlparse(get_settings().database_url)
    if url.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        pytest.skip("PostgreSQL integration test requires a PostgreSQL DATABASE_URL")
    try:
        with socket.create_connection((url.hostname or "localhost", url.port or 5432), timeout=2):
            pass
    except OSError as exc:
        pytest.skip(f"PostgreSQL unavailable ({exc})")


@pytest.fixture(autouse=True, scope="session")
def _dev_auth():
    """Флаг обхода читается один раз при создании настроек."""
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.dev_auth_bypass, "тесты должны ходить через dev-token"
    yield


@pytest.fixture(autouse=True, scope="session")
def _parsed_yaml_once():
    """Каждый `TestClient` на старте перечитывает всю библиотеку сценариев:
    ~200 YAML — это 0.2 с на тест и минута на прогон. Разбор кешируется на
    сессию; проверка схемы и сборка эталона идут по-прежнему на каждый старт.
    Копия — чтобы правка словаря в одном тесте не протекла в другой.
    """
    from app.scenarios import loader

    original = loader._read_yaml
    cache: dict[tuple[Path, int], dict] = {}

    def cached(path: Path) -> dict:
        key = (path.resolve(), path.stat().st_mtime_ns)
        if key not in cache:
            cache[key] = original(path)
        return copy.deepcopy(cache[key])

    loader._read_yaml = cached
    yield
    loader._read_yaml = original


@pytest.fixture(autouse=True)
def _fresh_engine():
    """Движок базы — на тест, а не на процесс.

    Пул соединений привязан к событийному циклу, в котором создан, а каждый
    `TestClient` поднимает свой. Общий движок даёт «Future attached to
    a different loop» на втором обращении к базе — тест падает не там, где
    ошибка, и только в компании других тестов.
    """
    from app.db import base

    base.reset()
    yield
    base.reset()


@pytest.fixture
def as_instructor(client):
    """Клиент, вошедший преподавателем. Cookie живёт в самом TestClient."""
    response = client.post("/api/auth/dev-token")
    assert response.status_code == 200, response.text
    return client
