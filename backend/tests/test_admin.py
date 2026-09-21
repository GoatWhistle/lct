"""АРМ администратора: учётные записи, состояние стенда, аудит, копии.

Пункты приёмки lct-24. Главное здесь — не функции, а границы роли: ТЗ прямо
запрещает администратору вмешиваться в оценки, и проверяется именно это.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.auth import Principal
from app.domain.roles import Role
from app.main import app
from app.session.hub import hub


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        hub.journal = None
        yield test_client


@pytest.fixture
def as_admin(client, monkeypatch):
    """Вход администратором. `dev-token` даёт преподавателя, поэтому роль
    подменяется на уровне проверки: заводить запись в базе ради прав не нужно."""
    client.post("/api/auth/dev-token")

    import app.api.auth as auth_module

    def admin_principal(request):
        return Principal(login="admin", full_name="Администратор", role=Role.ADMIN)

    monkeypatch.setattr(auth_module, "current", admin_principal)
    return client


@pytest.fixture
def as_instructor(client):
    client.post("/api/auth/dev-token")
    return client





def db_alive(client) -> bool:
    """Часть точек без Postgres работать не может, и это не повод падать:
    на машине разработчика база может быть не поднята."""
    return client.get("/api/health").status_code == 200


# ── границы роли ──


def test_instructor_cannot_open_admin(as_instructor):
    """Преподаватель не управляет учётными записями: ТЗ ограничивает его
    в административных функциях прямо."""
    assert as_instructor.get("/api/admin/users").status_code == 403
    assert as_instructor.get("/api/admin/audit").status_code == 403
    assert as_instructor.get("/api/admin/status").status_code == 403


def test_anonymous_cannot_open_admin(client):
    assert client.get("/api/admin/users").status_code == 401


def test_admin_has_no_way_to_change_a_score():
    """Запрет выражен отсутствием точки, а не проверкой внутри неё: в модуле
    администратора нет ни одного маршрута, трогающего оценки."""
    from app.api.http import admin

    paths = {route.path for route in admin.router.routes}
    assert not any("report" in path or "score" in path for path in paths)


def test_admin_cannot_write_to_the_audit_log():
    """ТЗ требует хранения журнала, а не управления им."""
    from app.api.http import admin

    methods = {
        (route.path, method)
        for route in admin.router.routes
        for method in getattr(route, "methods", set())
    }
    assert not [
        path for path, method in methods if "audit" in path and method in {"POST", "PATCH", "DELETE"}
    ]


# ── учётные записи ──


def test_admin_creates_a_trainee_with_a_trainee_card(as_admin):
    """У обучающегося должна появиться карточка курсанта: на ней висят
    профиль, история и проверка «это твой разбор» (lct-23)."""
    if not db_alive(as_admin):
        pytest.skip("нет базы")

    login = f"курсант-{uuid.uuid4().hex[:8]}"
    response = as_admin.post(
        "/api/admin/users",
        json={
            "login": login,
            "full_name": "Петров Пётр",
            "password": "длинный-пароль",
            "role": "trainee",
            "service": "ДДС района",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["role"] == "trainee"
    assert body["service"] == "ДДС района"

    listing = as_admin.get("/api/admin/users").json()
    assert any(user["login"] == login for user in listing)


def test_duplicate_login_is_refused(as_admin):
    if not db_alive(as_admin):
        pytest.skip("нет базы")

    login = f"двойник-{uuid.uuid4().hex[:8]}"
    payload = {
        "login": login, "full_name": "Первый", "password": "длинный-пароль", "role": "instructor",
    }
    assert as_admin.post("/api/admin/users", json=payload).status_code == 201
    second = as_admin.post("/api/admin/users", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"] == "login_taken"


def test_short_password_is_refused(as_admin):
    response = as_admin.post(
        "/api/admin/users",
        json={"login": "короткий", "full_name": "Кто-то", "password": "123", "role": "instructor"},
    )
    assert response.status_code == 422


def test_admin_cannot_block_himself(as_admin):
    """Иначе стенд остаётся без администратора до похода в базу руками."""
    if not db_alive(as_admin):
        pytest.skip("нет базы")

    created = as_admin.post(
        "/api/admin/users",
        json={
            "login": "admin", "full_name": "Администратор",
            "password": "длинный-пароль", "role": "admin",
        },
    )
    user_id = created.json()["id"] if created.status_code == 201 else next(
        user["id"] for user in as_admin.get("/api/admin/users").json() if user["login"] == "admin"
    )
    response = as_admin.patch(f"/api/admin/users/{user_id}", json={"blocked": True})
    assert response.status_code == 409
    assert response.json()["detail"] == "cannot_block_yourself"


# ── состояние стенда ──


def test_status_names_every_component(as_admin):
    if not db_alive(as_admin):
        pytest.skip("нет базы")

    names = {item["name"] for item in as_admin.get("/api/admin/status").json()}
    assert {"База данных", "Модели речи", "Эмбеддинги", "Провайдер LLM",
            "Классификатор ЕКП", "Секрет сессии"} <= names


def test_default_session_secret_is_reported_as_a_problem(as_admin):
    """На стенде это дыра, и увидеть её должен администратор, а не проверяющий."""
    if not db_alive(as_admin):
        pytest.skip("нет базы")

    secret = next(
        item for item in as_admin.get("/api/admin/status").json() if item["name"] == "Секрет сессии"
    )
    assert secret["ok"] is False, "значение по умолчанию не помечено как проблема"


# ── резервные копии ──


def test_backup_listing_is_open_to_admin(as_admin):
    assert as_admin.get("/api/admin/backups").status_code == 200


def test_backup_failure_explains_what_is_missing(as_admin, monkeypatch):
    """Кнопка не должна молча ничего не делать: если снять копию нечем,
    администратор видит, чего именно не хватает."""
    from app.admin import backup as backup_service

    def broken():
        raise backup_service.BackupError("нет ни pg_dump, ни docker")

    monkeypatch.setattr(backup_service, "create", broken)
    response = as_admin.post("/api/admin/backups")
    assert response.status_code == 503
    assert "pg_dump" in response.json()["detail"]
