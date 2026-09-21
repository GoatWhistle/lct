"""Общая настройка тестов.

Голосовой контур выключен: он грузит модели около десяти секунд, а проверяется
отдельно в `test_voice_pipeline.py`. Вход включён как на стенде, но тесты
логинятся через `dev-token` — так же, как `make lesson` (tasks/lct-23-auth-roles.md).
"""

import os

os.environ.setdefault("DEV_AUTH_BYPASS", "true")

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True, scope="session")
def _dev_auth():
    """Флаг обхода читается один раз при создании настроек."""
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.dev_auth_bypass, "тесты должны ходить через dev-token"
    yield


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
