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


@pytest.fixture
def as_instructor(client):
    """Клиент, вошедший преподавателем. Cookie живёт в самом TestClient."""
    response = client.post("/api/auth/dev-token")
    assert response.status_code == 200, response.text
    return client
