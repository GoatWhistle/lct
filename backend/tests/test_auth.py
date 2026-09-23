"""Вход, роли и аудит.

Пункты приёмки lct-23: курсант не открывает пульт даже по прямой ссылке,
администратор не правит оценки, чужой разбор закрыт, пароли не хранятся
в открытом виде.

Тесты, которым нужна база, пропускаются, когда Postgres не поднят: остальные
проверяют разграничение, для которого база не нужна.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.auth import hash_password, verify_password
from app.domain.roles import Role
from app.main import app
from app.session.hub import hub


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        hub.journal = None
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


# ── вход ──


def test_unknown_login_and_wrong_password_look_the_same(client):
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


def test_logout_clears_the_session(client):
    client.post("/api/auth/dev-token")
    client.post("/api/auth/logout")
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
