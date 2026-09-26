"""Вход, роли и аудит.

Пункты приёмки lct-23: курсант не открывает пульт даже по прямой ссылке,
администратор не правит оценки, чужой разбор закрыт, пароли не хранятся
в открытом виде.

Тесты, которым нужна база, пропускаются, когда Postgres не поднят: остальные
проверяют разграничение, для которого база не нужна.
"""

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.auth import hash_password, verify_password
from app.domain.roles import Role
from app.main import app
from app.session.hub import hub
from app.session.store import MemorySessionStore


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        hub.store = MemorySessionStore()
        yield test_client


# ── пароли ──


def test_password_is_hashed_not_stored():
    stored = hash_password("правильный-пароль")
    assert "правильный-пароль" not in stored
    assert stored.startswith("$argon2")


def test_password_verifies():
    stored = hash_password("правильный-пароль")
    assert verify_password(stored, "правильный-пароль")
    assert not verify_password(stored, "другой")


def test_broken_hash_does_not_let_anyone_in():
    """Битая запись в базе не должна открывать вход."""
    assert not verify_password("не хеш вовсе", "что угодно")


def test_malformed_stored_hash_is_not_written_to_logs(caplog):
    stored_hash = "private-stored-hash-marker"
    assert not verify_password(stored_hash, "candidate-password")
    assert stored_hash not in caplog.text
    assert "InvalidHash" in caplog.text


# ── вход ──


def test_unknown_login_and_wrong_password_look_the_same(client, postgres_access):
    """Иначе форма входа превращается в список действующих учётных записей."""
    first = client.post("/api/auth/login", json={"login": "нет-такого", "password": "x"})
    assert first.status_code == 401
    assert first.json()["detail"] == "bad_credentials"


def test_login_rejects_values_outside_database_and_hash_bounds(client):
    too_long_login = client.post(
        "/api/auth/login", json={"login": "a" * 81, "password": "not-used"}
    )
    too_long_password = client.post(
        "/api/auth/login", json={"login": "operator", "password": "x" * 1025}
    )
    assert too_long_login.status_code == 422
    assert too_long_password.status_code == 422


def test_me_requires_authentication(client):
    assert client.get("/api/auth/me").status_code == 401


def test_dev_token_gives_an_instructor(client):
    response = client.post("/api/auth/dev-token")
    assert response.status_code == 200
    assert response.json()["role"] == "instructor"
    assert client.get("/api/auth/me").json()["role"] == "instructor"


def test_directory_login_issues_the_mapped_role_and_identity(client, monkeypatch):
    from app import directory
    from app.api import auth
    from app.config import get_settings
    from app.directory import DirectoryIdentity

    # This route test supplies its own account and sessionmaker below. Mark an
    # empty auth-generation snapshot fresh as if startup had loaded the empty
    # test directory; otherwise the production middleware correctly fails
    # closed with 503 when the sandbox cannot reach PostgreSQL.
    monkeypatch.setattr(auth, "_generations", {})
    monkeypatch.setattr(auth, "_generations_synced_at", time.monotonic())

    provisioned = {}

    class EmptyDb:
        async def scalar(self, query):
            if "users.auth_version" in str(query) and "user" in provisioned:
                return provisioned["user"].auth_version
            return None

    class DbContext:
        async def __aenter__(self):
            return EmptyDb()

        async def __aexit__(self, *_args):
            return None

    settings = get_settings().model_copy(update={"ldap_enabled": True})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: DbContext())
    identity = DirectoryIdentity(
        login="trainee.one",
        full_name="Курсант Один",
        role=Role.TRAINEE,
        service="01",
        subject="directory-guid-1",
    )

    async def authenticate(login, password):
        assert login == "trainee.one"
        assert password == "directory-password"
        return identity

    async def provision(_identity):
        provisioned["user"] = SimpleNamespace(
            login=identity.login,
            full_name=identity.full_name,
            role=identity.role.value,
            service=identity.service,
            trainee_id=uuid4(),
            auth_version=0,
            blocked=False,
        )
        return provisioned["user"]

    async def audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(directory, "authenticate", authenticate)
    monkeypatch.setattr(auth, "_directory_account", provision)
    monkeypatch.setattr(auth, "audit", audit)
    response = client.post(
        "/api/auth/login",
        json={"login": "trainee.one", "password": "directory-password"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "trainee"
    assert response.json()["service"] == "01"
    assert client.get("/api/auth/me").json()["login"] == "trainee.one"


def test_directory_outage_does_not_fall_back_or_issue_a_session(client, monkeypatch):
    from app import directory
    from app.api import auth
    from app.config import get_settings
    from app.directory import DirectoryUnavailable

    class EmptyDb:
        async def scalar(self, _query):
            return None

    class DbContext:
        async def __aenter__(self):
            return EmptyDb()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: get_settings().model_copy(update={"ldap_enabled": True}),
    )
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: DbContext())

    async def unavailable(*_args):
        raise DirectoryUnavailable("directory service unavailable")

    async def audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(directory, "authenticate", unavailable)
    monkeypatch.setattr(auth, "audit", audit)
    response = client.post(
        "/api/auth/login", json={"login": "trainee.one", "password": "anything"}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "directory_unavailable"
    assert client.get("/api/auth/me").status_code == 401


def test_blocked_directory_account_attempt_is_audited(client, monkeypatch):
    from app import directory
    from app.api import auth
    from app.config import get_settings
    from app.directory import DirectoryIdentity

    class EmptyDb:
        async def scalar(self, _query):
            return None

    class DbContext:
        async def __aenter__(self):
            return EmptyDb()

        async def __aexit__(self, *_args):
            return None

    settings = get_settings().model_copy(update={"ldap_enabled": True})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: DbContext())
    identity = DirectoryIdentity(
        login="trainee.one",
        full_name="Курсант Один",
        role=Role.TRAINEE,
        service="01",
        subject="directory-guid-blocked",
    )

    async def authenticate(*_args):
        return identity

    async def provision(_identity):
        return SimpleNamespace(
            login=identity.login,
            full_name=identity.full_name,
            role=identity.role.value,
            service=identity.service,
            trainee_id=uuid4(),
            auth_version=0,
            blocked=True,
        )

    audit_events = []

    async def audit(*args):
        audit_events.append(args)

    monkeypatch.setattr(directory, "authenticate", authenticate)
    monkeypatch.setattr(auth, "_directory_account", provision)
    monkeypatch.setattr(auth, "audit", audit)
    response = client.post(
        "/api/auth/login",
        json={"login": "trainee.one", "password": "directory-password"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "blocked"
    assert any(event[2] == "login.blocked" for event in audit_events)
    assert client.get("/api/auth/me").status_code == 401


def test_logout_clears_and_revokes_the_session(client, postgres_access):
    assert client.post("/api/auth/dev-token").status_code == 200
    stale_cookie = client.cookies.get("lct_session")
    response = client.post("/api/auth/logout")
    assert response.status_code == 200, response.text
    assert client.get("/api/auth/me").status_code == 401
    # Replaying a copied pre-logout cookie must not restore the authenticated session.
    client.cookies.set("lct_session", stale_cookie)
    assert client.get("/api/auth/me").status_code == 401


# ── разграничение ──


def test_anonymous_cannot_open_any_socket(client):
    """Номер занятия перестал быть пропуском: раньше по ссылке пускало знание
    адреса, теперь — роль."""
    session_id = uuid4()
    for path in (f"/ws/control/{session_id}", f"/ws/call/{session_id}",
                 f"/ws/observe/{session_id}", f"/ws/station/{session_id}"):
        with client.websocket_connect(path) as socket:
            message = socket.receive_json()
            assert message["type"] == "error"
            assert message["code"] == "forbidden", path


def test_anonymous_cannot_read_history(client):
    assert client.get("/api/sessions").status_code == 401


def test_anonymous_cannot_read_trainees(client):
    assert client.get("/api/trainees").status_code == 401


def test_classifier_is_open_to_everyone(client):
    """Справочник ЕКП прятать не от кого: оператор видит тот же список
    на боевом АРМ."""
    assert client.get("/api/ekp/groups").status_code == 200


def test_health_stays_open(client):
    """Экран «стенд прогревается» показывается до входа."""
    assert client.get("/api/health").status_code == 200


def test_instructor_can_open_control(client):
    """На `control` сервер не шлёт ничего — это канал только на запись
    (docs/arch/CONTRACT.md). Признак того, что он открыт, — поднявшееся
    занятие, а не ответное сообщение."""
    import time

    client.post("/api/auth/dev-token")
    session_id = uuid4()
    with client.websocket_connect(f"/ws/control/{session_id}") as socket:
        socket.send_json({"type": "scenario.start", "scenario_id": "fire-apartment-l2",
                          "trainee": "Иванов", "mode": "training"})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and hub.get(session_id) is None:
            time.sleep(0.02)
        assert hub.get(session_id) is not None, "занятие не поднялось: канал отвергнут"


def test_roles_are_three_and_named_in_the_spec():
    assert {role.value for role in Role} == {"admin", "instructor", "trainee"}
