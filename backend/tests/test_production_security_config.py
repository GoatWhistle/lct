import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings


def prod_settings(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "database_url": "postgresql+asyncpg://lct:postgres-secret-with-more-than-32-characters@localhost:5432/lct",
        "session_secret": "a-unique-secret-that-is-at-least-32-characters-long",
        "secure_cookies": True,
        "dev_auth_bypass": False,
        "offline": True,
        "llm_provider": "local",
        **overrides,
    }
    return Settings(_env_file=None, **values)


def test_production_accepts_unique_secret_https_cookie_and_password_auth():
    prod_settings().validate_deployment_security()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"session_secret": "dev-secret-поменять-на-стенде"}, "SESSION_SECRET"),
        ({"session_secret": "short"}, "SESSION_SECRET"),
        ({"database_url": "postgresql+asyncpg://lct:short@localhost:5432/lct"}, "PostgreSQL password"),
        ({"database_url": "postgresql+asyncpg://lct:has%40unsafe%40characters-over-32@localhost:5432/lct"}, "PostgreSQL password"),
        ({"secure_cookies": False}, "SECURE_COOKIES"),
        ({"dev_auth_bypass": True}, "DEV_AUTH_BYPASS"),
        ({"demo_no_db": True}, "DEMO_NO_DB"),
        ({"offline": False}, "OFFLINE"),
        ({"llm_provider": "openai"}, "LLM_PROVIDER"),
    ],
)
def test_production_rejects_insecure_authentication_defaults(overrides, message):
    with pytest.raises(ValueError, match=message):
        prod_settings(**overrides).validate_deployment_security()


def test_development_keeps_local_http_and_dev_token_available():
    settings = Settings(_env_file=None, app_env="development")
    settings.validate_deployment_security()


def test_production_app_startup_fails_before_serving_with_default_secret(monkeypatch):
    settings = prod_settings(session_secret="dev-secret-поменять-на-стенде")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        with TestClient(main.app):
            pass
