"""АРМ администратора: учётные записи, состояние стенда, аудит, копии.

Пункты приёмки lct-24. Главное здесь — не функции, а границы роли: ТЗ прямо
запрещает администратору вмешиваться в оценки, и проверяется именно это.
"""

import uuid
from xml.etree import ElementTree as ET

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
    assert as_instructor.get("/api/admin/diagnostics").status_code == 403
    assert as_instructor.get("/api/admin/config.xml").status_code == 403


def test_anonymous_cannot_open_admin(client):
    assert client.get("/api/admin/users").status_code == 401


def test_admin_downloads_safe_xml_configuration(as_admin):
    response = as_admin.get("/api/admin/config.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert "attachment" in response.headers["content-disposition"]
    root = ET.fromstring(response.content)
    assert root.tag == "lctConfiguration"
    assert root.find("./workstations/workstation[@role='admin']") is not None
    assert root.find("./timerLimits/timer[@code='dds_ack']") is not None
    lowered = response.content.lower()
    assert b"session_secret" not in lowered
    assert b"llm_api_key" not in lowered
    assert b"database_url" not in lowered


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


def test_audit_api_applies_actor_action_and_offset_filters(as_admin):
    """The admin UI must be able to reach older records, not just page zero."""
    from app.api.http import admin as admin_module

    class FakeDb:
        statement = None

        async def scalars(self, statement):
            self.statement = statement
            return []

    fake_db = FakeDb()

    async def fake_session():
        yield fake_db

    app.dependency_overrides[admin_module.get_session] = fake_session
    try:
        response = as_admin.get(
            "/api/admin/audit",
            params={"action": "login.failed", "actor": "alice", "limit": 201, "offset": 400},
        )
    finally:
        app.dependency_overrides.pop(admin_module.get_session, None)

    assert response.status_code == 200, response.text
    statement = fake_db.statement
    assert statement._limit_clause.value == 201
    assert statement._offset_clause.value == 400
    assert "audit_log.action" in str(statement.whereclause)
    assert "audit_log.actor" in str(statement.whereclause)


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
            "Классификатор ЕКП", "Резервное копирование", "Секрет сессии",
            "Нагрузка backend"} <= names


def test_admin_gets_live_metrics_and_downloadable_failure_report(as_admin):
    if not db_alive(as_admin):
        pytest.skip("нет базы")

    response = as_admin.get("/api/admin/diagnostics")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metrics"]["cpu_cores"] >= 1
    assert body["metrics"]["disk_total_bytes"] > body["metrics"]["disk_free_bytes"]
    assert body["metrics"]["active_sessions"] >= 0
    assert isinstance(body["recent_system_events"], list)
    assert isinstance(body["failed_actions_24h"], list)

    download = as_admin.get("/api/admin/diagnostics.json")
    assert download.status_code == 200
    assert "attachment" in download.headers["content-disposition"]


def test_diagnostic_journal_redacts_credentials():
    import logging

    from app.monitoring import recent_events

    logging.getLogger("app.redaction-test").warning(
        "failure postgresql://user:secret@db/lct SESSION_SECRET=never-show"
    )
    event = recent_events(limit=1)[0]
    assert "user:secret@" not in event["message"]
    assert "never-show" not in event["message"]


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


def test_backup_listing_failure_does_not_break_admin_status(as_admin, monkeypatch):
    from app.admin import backup as backup_service

    def broken():
        raise backup_service.BackupError("каталог временно недоступен")

    monkeypatch.setattr(backup_service, "listing", broken)
    response = as_admin.get("/api/admin/status")
    assert response.status_code == 200, response.text
    backup = next(item for item in response.json() if item["name"] == "Резервное копирование")
    assert not backup["ok"]
    assert "каталог временно недоступен" in backup["detail"]


def test_backup_list_returns_503_on_storage_error(as_admin, monkeypatch):
    from app.admin import backup as backup_service

    def broken():
        raise backup_service.BackupError("каталог временно недоступен")

    monkeypatch.setattr(backup_service, "listing", broken)
    response = as_admin.get("/api/admin/backups")
    assert response.status_code == 503
    assert "каталог временно недоступен" in response.json()["detail"]


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


def test_pg17_transaction_timeout_is_removed_from_pg16_backup(monkeypatch, tmp_path):
    """Образ backend может содержать клиент новее сервера из Compose."""
    from app.admin import backup as backup_service

    monkeypatch.setattr(backup_service, "DIR", tmp_path)
    monkeypatch.setattr(backup_service, "_dsn", lambda: ("u", "p", "db", "5432", "lct"))
    monkeypatch.setattr(backup_service.shutil, "which", lambda name: "/usr/bin/pg_dump")

    class Result:
        returncode = 0
        stderr = b""
        stdout = (b"--\n-- PostgreSQL database dump\n--\n\n"
                  b"SET transaction_timeout = 0;\nCREATE TABLE demo(id integer);\n")

    monkeypatch.setattr(backup_service.subprocess, "run", lambda *args, **kwargs: Result())
    made = backup_service.create()
    content = (tmp_path / made["name"]).read_bytes()
    assert b"transaction_timeout" not in content
    assert b"CREATE TABLE demo" in content


def test_docker_backup_does_not_put_database_password_in_process_arguments(monkeypatch, tmp_path):
    import base64
    from types import SimpleNamespace

    from app.admin import backup as backup_service

    password = "secret p@ss:word"
    monkeypatch.setattr(backup_service, "DIR", tmp_path)
    monkeypatch.setattr(backup_service, "_dsn", lambda: ("backup", password, "db", "5432", "lct"))
    monkeypatch.setattr(
        backup_service.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return SimpleNamespace(
            returncode=0,
            stderr=b"",
            stdout=b"--\n-- PostgreSQL database dump\nCREATE TABLE demo(id integer);\n",
        )

    monkeypatch.setattr(backup_service.subprocess, "run", fake_run)
    backup_service.create()

    assert password not in " ".join(captured["command"])
    assert captured["input"] == base64.b64encode(password.encode()) + b"\n"


def test_backup_dsn_decodes_escaped_credentials_without_exposing_them(monkeypatch):
    from types import SimpleNamespace
    from app.admin import backup as backup_service

    monkeypatch.setattr(
        backup_service, "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://backup:p%40ss%3Aword@db.example:5433/lct"),
    )
    assert backup_service._dsn() == ("backup", "p@ss:word", "db.example", "5433", "lct")

    monkeypatch.setattr(
        backup_service, "get_settings",
        lambda: SimpleNamespace(database_url="not-a-valid-url-with-secret"),
    )
    try:
        backup_service._dsn()
    except backup_service.BackupError as exc:
        assert "secret" not in str(exc)
    else:
        raise AssertionError("invalid DATABASE_URL must be rejected")


def test_backup_directory_failure_is_retryable_backup_error(monkeypatch, tmp_path):
    from app.admin import backup as backup_service

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("occupied")
    monkeypatch.setattr(backup_service, "DIR", blocker / "backups")
    monkeypatch.setattr(backup_service, "_dsn", lambda: ("u", "p", "db", "5432", "lct"))

    with pytest.raises(backup_service.BackupError, match="каталог резервных копий"):
        backup_service.create()


def test_backup_listing_io_failure_is_retryable_error(monkeypatch, tmp_path):
    from app.admin import backup as backup_service

    monkeypatch.setattr(backup_service, "DIR", tmp_path)

    def denied(_path):
        raise PermissionError("access denied")

    monkeypatch.setattr(backup_service.os, "scandir", denied)
    with pytest.raises(backup_service.BackupError, match="прочитать каталог"):
        backup_service.listing()


def test_windows_backup_permissions_use_explicit_private_acl(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.admin import backup as backup_service

    monkeypatch.setattr(backup_service, "getuser", lambda: "operator")
    monkeypatch.setenv("USERDOMAIN", "TRAINING")
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    called = {}

    def fake_run(command, **kwargs):
        called["command"] = command
        called["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(backup_service.subprocess, "run", fake_run)
    backup_service._restrict_file_permissions(-1, tmp_path / "copy.sql", platform_name="nt")

    command = called["command"]
    assert command[0].endswith("icacls.exe")
    assert "/inheritance:r" in command
    assert "TRAINING\\operator:(F)" in command
    assert "*S-1-5-18:(F)" in command
    assert called["kwargs"]["timeout"] == 15
