"""Regressions for stale cookies and privileged admin operations."""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.api.auth as auth
from app.api.http import admin
from app.domain.roles import Role
from app.main import app
from app.session.hub import hub


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        hub.journal = None
        yield test_client


def test_account_change_revokes_http_and_new_websocket_handshakes(client):
    assert client.post("/api/auth/dev-token").status_code == 200
    assert client.get("/api/auth/me").status_code == 200
    auth.invalidate_login("dev")
    assert client.get("/api/auth/me").status_code == 401
    with client.websocket_connect(f"/ws/control/{uuid4()}") as socket:
        assert socket.receive_json()["code"] == "forbidden"
    assert client.post("/api/auth/dev-token").status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_account_change_closes_an_existing_websocket(client):
    assert client.post("/api/auth/dev-token").status_code == 200
    with client.websocket_connect(f"/ws/control/{uuid4()}") as socket:
        auth.invalidate_login("dev")
        with pytest.raises(WebSocketDisconnect) as exc:
            socket.receive_json()
        assert exc.value.code == 1008


def test_cookie_from_previous_process_is_rejected(client, monkeypatch):
    assert client.post("/api/auth/dev-token").status_code == 200
    monkeypatch.setattr(auth, "_INSTANCE", "new-server-instance")
    assert client.get("/api/auth/me").status_code == 401


class FakeDb:
    def __init__(self, user):
        self.user = user
        self.commits = 0
        self.added = []

    async def get(self, _model, _id):
        return self.user

    def add(self, object_):
        self.added.append(object_)

    async def flush(self):
        for object_ in self.added:
            if getattr(object_, "id", None) is None:
                object_.id = uuid4()

    async def commit(self):
        self.commits += 1


def fake_user(login="victim", role="instructor"):
    return SimpleNamespace(
        id=uuid4(), login=login, full_name="Проверка", role=role,
        service=None, trainee_id=None, blocked=False,
        password_hash="old", created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_admin_patch_revokes_cookie_after_commit(monkeypatch):
    user = fake_user()
    db = FakeDb(user)
    calls = []
    monkeypatch.setattr(admin, "require", lambda _request, *_roles: auth.Principal(
        login="admin", full_name="Администратор", role=Role.ADMIN,
    ))
    monkeypatch.setattr(admin, "invalidate_login", lambda login: calls.append((login, db.commits)))

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(admin, "audit", no_audit)
    await admin.patch_user(user.id, admin.UserPatch(blocked=True), object(), db)
    assert user.blocked is True
    assert calls == [("victim", 1)]


@pytest.mark.asyncio
async def test_admin_cannot_demote_self(monkeypatch):
    user = fake_user(login="admin", role="admin")
    db = FakeDb(user)
    monkeypatch.setattr(admin, "require", lambda _request, *_roles: auth.Principal(
        login="admin", full_name="Администратор", role=Role.ADMIN,
    ))
    with pytest.raises(HTTPException) as exc:
        await admin.patch_user(user.id, admin.UserPatch(role=Role.TRAINEE), object(), db)
    assert exc.value.status_code == 409
    assert db.commits == 0


@pytest.mark.asyncio
async def test_promotion_to_trainee_creates_profile(monkeypatch):
    user = fake_user()
    db = FakeDb(user)
    monkeypatch.setattr(admin, "require", lambda _request, *_roles: auth.Principal(
        login="admin", full_name="Администратор", role=Role.ADMIN,
    ))

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(admin, "audit", no_audit)
    await admin.patch_user(user.id, admin.UserPatch(role=Role.TRAINEE), object(), db)
    assert user.role == "trainee"
    assert user.trainee_id is not None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_backup_runs_off_event_loop_and_failure_is_audited(monkeypatch):
    who = auth.Principal(login="admin", full_name="Администратор", role=Role.ADMIN)
    monkeypatch.setattr(admin, "require", lambda _request, *_roles: who)
    calls = []

    async def fake_threadpool(fn):
        calls.append(fn)
        return fn()

    async def fake_audit(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(admin, "run_in_threadpool", fake_threadpool)
    monkeypatch.setattr(admin, "audit", fake_audit)
    monkeypatch.setattr(admin.backup_service, "create", lambda: {
        "name": "example.sql", "size_bytes": 1, "at": datetime.now(timezone.utc),
    })
    assert (await admin.make_backup(object())).name == "example.sql"
    assert calls[0] is admin.backup_service.create

    def broken():
        raise admin.backup_service.BackupError("pg_dump failed")

    monkeypatch.setattr(admin.backup_service, "create", broken)
    with pytest.raises(HTTPException) as exc:
        await admin.make_backup(object())
    assert exc.value.status_code == 503
    assert any(isinstance(item, tuple) and item[0][2] == "backup.failed" for item in calls)


def test_backup_error_redacts_database_credentials(monkeypatch):
    dsn = "postgresql://user:supersecret@localhost:5432/example"
    monkeypatch.setattr(admin, "get_settings", lambda: SimpleNamespace(database_url=dsn))
    detail = admin._safe_backup_error(admin.backup_service.BackupError(f"bad DSN: {dsn}"))
    assert "supersecret" not in detail
    assert dsn not in detail
