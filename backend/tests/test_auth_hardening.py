"""Regressions for stale cookies and privileged admin operations."""

import asyncio
import re
import weakref
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import auth
from app.api.http import admin
from app.domain.roles import Role
from app.main import app
from app.session.hub import hub
from app.session.store import MemorySessionStore


@pytest.mark.parametrize(
    "headers, scope, expected",
    [
        ({"origin": "http://training.lan", "host": "training.lan"}, {"scheme": "ws"}, True),
        (
            {
                "origin": "https://training.lan:5443",
                "host": "backend:8000",
                "x-forwarded-host": "training.lan:5443",
                "x-forwarded-proto": "https",
            },
            {"scheme": "ws"},
            True,
        ),
        ({"origin": "https://attacker.invalid", "host": "training.lan"}, {"scheme": "ws"}, False),
        ({"origin": "http://training.lan:5173", "host": "training.lan:8000"}, {"scheme": "ws"}, False),
        (
            {
                "origin": "http://training.lan",
                "host": "backend:8000",
                "x-forwarded-host": "training.lan",
                "x-forwarded-proto": "https",
            },
            {"scheme": "wss"},
            False,
        ),
        ({"host": "training.lan"}, {"scheme": "ws"}, True),
        ({"origin": "not a URL", "host": "training.lan"}, {"scheme": "ws"}, False),
    ],
)
def test_websocket_origin_policy(headers, scope, expected):
    assert auth.websocket_origin_allowed(SimpleNamespace(headers=headers, scope=scope)) is expected


def test_nginx_proxies_preserve_external_host_for_websocket_origin_validation():
    project_root = Path(__file__).resolve().parents[2]
    for config in ("nginx.conf.template", "nginx.tls.conf.template"):
        text = (project_root / "frontend" / config).read_text(encoding="utf-8")
        match = re.search(r"location /ws/ \{(.*?)^    \}", text, re.MULTILINE | re.DOTALL)
        assert match is not None, f"{config}: missing WebSocket proxy block"
        websocket_location = match.group(1)
        assert "proxy_set_header X-Forwarded-Host $http_host;" in websocket_location
    tls = (project_root / "frontend" / "nginx.tls.conf.template").read_text(encoding="utf-8")
    match = re.search(r"location /ws/ \{(.*?)^    \}", tls, re.MULTILINE | re.DOTALL)
    assert match is not None
    tls_websocket_location = match.group(1)
    assert "proxy_set_header X-Forwarded-Proto https;" in tls_websocket_location


def test_cluster_nginx_pins_all_session_channels_and_session_apis_to_one_hash_key():
    project_root = Path(__file__).resolve().parents[2]
    for config in ("nginx.cluster.conf.template", "nginx.cluster.tls.conf.template"):
        text = (project_root / "frontend" / config).read_text(encoding="utf-8")
        assert "hash $session_route_key consistent;" in text
        assert "server backend:8000" in text and "server backend-b:8000" in text
        assert re.search(
            r"~\^/ws/\(\?:control\|call\|observe\|station\)/\(\[0-9a-fA-F-\]\{36\}\)",
            text,
        ), f"{config}: all WebSocket channels must extract the same session UUID"
        assert re.search(
            r"~\^/api/sessions/\(\[0-9a-fA-F-\]\{36\}\)", text
        ), f"{config}: session REST endpoints must use the same routing key"
        assert text.count("proxy_pass http://backend_cluster;") == 2


@pytest.fixture
def client():
    # Each TestClient represents a fresh backend process.  In particular,
    # revocations exercised by a previous test must not leak into the next
    # process' in-memory cache.
    auth.prime_generations({})
    with TestClient(app) as test_client:
        hub.store = MemorySessionStore()
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


def test_generation_sync_preserves_synthetic_dev_account(monkeypatch):
    class FakeResult:
        def all(self):
            return []

    class FakeDb:
        async def execute(self, _query):
            return FakeResult()

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    async def run():
        auth.prime_generations({"dev": 7})
        await auth.sync_generations()
        assert auth._generations["dev"] == 7
        auth._generations.pop("dev", None)

    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(dev_auth_bypass=True))
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: FakeSession())
    asyncio.run(run())


@pytest.mark.asyncio
async def test_stale_generation_snapshot_does_not_revoke_newly_resolved_login(monkeypatch):
    snapshot_read = asyncio.Event()
    release_snapshot = asyncio.Event()

    class FakeResult:
        def all(self):
            return []

    class FakeDb:
        async def execute(self, _query):
            snapshot_read.set()
            await release_snapshot.wait()
            return FakeResult()

        async def scalar(self, _query):
            return 0

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    auth.prime_generations({})
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(dev_auth_bypass=False))
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: FakeSession())
    sync = asyncio.create_task(auth.sync_generations())
    await snapshot_read.wait()
    assert await auth._resolve_unknown_login("just-created") == 0
    release_snapshot.set()
    await sync
    assert auth._generations["just-created"] == 0
    assert "just-created" not in auth._vanished


@pytest.mark.asyncio
async def test_stale_generation_snapshot_does_not_restore_revoked_cookie(monkeypatch):
    snapshot_read = asyncio.Event()
    release_snapshot = asyncio.Event()

    class FakeResult:
        def all(self):
            return [("revoked", 0)]

    class FakeDb:
        async def execute(self, _query):
            snapshot_read.set()
            await release_snapshot.wait()
            return FakeResult()

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    auth.prime_generations({"revoked": 0})
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(dev_auth_bypass=False))
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: FakeSession())
    sync = asyncio.create_task(auth.sync_generations())
    await snapshot_read.wait()
    auth.invalidate_login("revoked", 1)
    release_snapshot.set()
    await sync
    assert auth._generations["revoked"] == 1


def test_cross_origin_browser_websocket_is_rejected_before_handshake(client):
    assert client.post("/api/auth/dev-token").status_code == 200
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/ws/control/{uuid4()}", headers={"origin": "https://attacker.invalid"}
        ):
            pytest.fail("cross-origin websocket must not be accepted")
    assert exc.value.code == 1008


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


def test_cookie_survives_generation_cache_reload_when_account_is_unchanged(client):
    assert client.post("/api/auth/dev-token").status_code == 200
    auth.prime_generations({"dev": 0})
    assert client.get("/api/auth/me").status_code == 200


def test_login_is_not_issued_when_security_audit_cannot_be_written(client, monkeypatch):
    from app.config import get_settings

    user = SimpleNamespace(
        login="audit-login", auth_provider="local", password_hash="hash",
        blocked=False, role="instructor", full_name="Преподаватель",
        service=None, trainee_id=None, auth_version=0,
    )

    class FakeDb:
        async def scalar(self, _statement):
            return user

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    settings = get_settings().model_copy(update={"demo_no_db": False, "ldap_enabled": False})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: FakeSession())
    monkeypatch.setattr(auth, "verify_password", lambda *_args: True)

    async def audit_failure(*_args, **_kwargs):
        return False

    monkeypatch.setattr(auth, "audit", audit_failure)
    response = client.post(
        "/api/auth/login", json={"login": user.login, "password": "valid"}
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "audit_unavailable"
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.asyncio
async def test_audit_storage_failure_does_not_log_user_supplied_detail(caplog, monkeypatch):
    settings = auth.get_settings().model_copy(update={"demo_no_db": False})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)

    def broken_session():
        raise RuntimeError("private-user-comment-must-not-reach-logs")

    monkeypatch.setattr(auth, "get_sessionmaker", lambda: broken_session)
    assert not await auth.audit(
        "teacher", "instructor", "score.override", "session-id",
        "sensitive comment must not be logged",
    )
    assert "private-user-comment-must-not-reach-logs" not in caplog.text
    assert "sensitive comment" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_demo_logout_revokes_replayed_cookie(client, monkeypatch):
    settings = auth.get_settings().model_copy(update={"demo_no_db": True})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    assert client.post("/api/auth/dev-token").status_code == 200
    stale_cookie = client.cookies.get("lct_session")
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.set("lct_session", stale_cookie)
    assert client.get("/api/auth/me").status_code == 401


def test_peer_node_generation_sync_closes_revoked_websocket(monkeypatch):
    login = "peer-revoked"

    class FakeResult:
        def all(self):
            return [(login, 4)]

    class FakeDb:
        async def execute(self, _query):
            return FakeResult()

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    class FakeSocket:
        closed = False

        async def close(self, **_kwargs):
            self.closed = True

    async def run():
        auth.prime_generations({login: 3})
        socket = FakeSocket()
        auth._active_sockets[login] = weakref.WeakKeyDictionary({
            socket: asyncio.get_running_loop(),
        })
        await auth.sync_generations()
        await asyncio.sleep(0.01)
        assert auth._generations[login] == 4
        assert socket.closed
        auth._active_sockets.pop(login, None)
        auth._generations.pop(login, None)

    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: FakeSession())
    asyncio.run(run())


def test_generation_sync_keeps_revocation_of_login_missing_from_db(monkeypatch):
    # Демо-логины синтетические, в users их нет. Сброс поколения к 0 после
    # выхода вернул бы силу cookie, выданной до выхода.
    login = "demo-instructor"

    class FakeResult:
        def all(self):
            return []

    class FakeDb:
        async def execute(self, _query):
            return FakeResult()

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: FakeSession())
    auth.prime_generations({})
    who = auth._demo_principal(Role.INSTRUCTOR)
    request = SimpleNamespace(session={})
    auth._issue_session(request, who)
    stale = dict(request.session)

    auth.invalidate_login(login)
    asyncio.run(auth.sync_generations())

    assert auth._session_principal(stale) is None
    auth._generations.pop(login, None)


def test_revocation_does_not_log_error_if_socket_already_disconnected():
    class DisconnectedSocket:
        async def close(self, **_kwargs):
            raise WebSocketDisconnect(code=1006)

    asyncio.run(auth._close_revoked(DisconnectedSocket()))


def test_auth_middleware_rejects_cookie_with_old_database_epoch(monkeypatch):
    login = "stale-cookie"

    class FakeDb:
        async def scalar(self, _query):
            return 5

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    observed = {}

    class InnerApp:
        async def __call__(self, scope, _receive, _send):
            observed["session"] = dict(scope["session"])

    async def run():
        auth.prime_generations({login: 5})
        cookie_session = {
            "principal": {"login": login},
            "auth_instance": auth._INSTANCE,
            "auth_generation": 4,
        }
        scope = {"type": "http", "session": cookie_session}
        async def unused_receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def unused_send(_message):
            return None
        middleware = auth.AuthVersionMiddleware(InnerApp())
        await middleware(scope, unused_receive, unused_send)
        assert observed["session"] == {}
        auth._generations.pop(login, None)

    settings = auth.get_settings().model_copy(update={"demo_no_db": False})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: FakeSession())
    asyncio.run(run())


def test_auth_middleware_fails_closed_when_generation_cache_is_stale_but_allows_logout(monkeypatch):
    from app.config import get_settings

    class BrokenSession:
        async def __aenter__(self):
            raise OSError("database unavailable")

        async def __aexit__(self, *_args):
            return None

    class InnerApp:
        def __init__(self):
            self.called = False

        async def __call__(self, _scope, _receive, _send):
            self.called = True

    async def run():
        login = "db-outage-user"
        auth.prime_generations({login: 0})
        monkeypatch.setattr(
            auth, "_generations_synced_at",
            auth.time.monotonic() - auth.AUTH_GENERATION_MAX_AGE_SECONDS - 1,
        )
        principal = auth.Principal(
            login=login, full_name="Учётная запись", role=Role.INSTRUCTOR
        )
        scope = {"type": "http", "path": "/api/admin/users", "session": {
            "principal": principal.model_dump(mode="json"),
            "auth_instance": auth._INSTANCE,
            "auth_generation": 0,
        }}
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        protected = InnerApp()
        await auth.AuthVersionMiddleware(protected)(scope, receive, send)
        assert not protected.called
        assert messages[0]["status"] == 503

        logout_scope = {**scope, "path": "/api/auth/logout", "session": dict(scope["session"])}
        logout = InnerApp()
        await auth.AuthVersionMiddleware(logout)(logout_scope, receive, send)
        assert logout.called, "logout must reach the route so it can clear the cookie"

    settings = get_settings().model_copy(update={"demo_no_db": False, "dev_auth_bypass": False})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: BrokenSession())
    asyncio.run(run())


def test_middleware_resolves_and_caches_login_unknown_to_this_node(monkeypatch):
    """Cluster handshake (lct-42): a login another node just authenticated is
    fetched via a single SELECT rather than being treated as revoked, and the
    result is cached so a second request for it does not query again."""
    login = "peer-node-fresh-login"
    calls = []

    class FakeDb:
        async def scalar(self, _query):
            calls.append(1)
            return 5

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    class InnerApp:
        def __init__(self):
            self.called = 0

        async def __call__(self, _scope, _receive, _send):
            self.called += 1

    async def run():
        auth.prime_generations({})

        def make_scope():
            return {
                "type": "http", "path": "/api/admin/users",
                "session": {
                    "principal": {"login": login},
                    "auth_instance": auth._INSTANCE,
                    "auth_generation": 5,
                },
            }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        first = InnerApp()
        await auth.AuthVersionMiddleware(first)(make_scope(), receive, send)
        assert first.called == 1, "a fresh, valid epoch must reach the route"
        assert auth._generations[login] == 5
        assert calls == [1]

        second = InnerApp()
        await auth.AuthVersionMiddleware(second)(make_scope(), receive, send)
        assert second.called == 1
        assert calls == [1], "cached epoch must not trigger a second SELECT"

    settings = auth.get_settings().model_copy(update={"demo_no_db": False})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: FakeSession())
    try:
        asyncio.run(run())
    finally:
        auth._generations.pop(login, None)
        auth._lookup_locks.pop(login, None)


def test_middleware_rejects_login_missing_from_users_via_one_shot_query(monkeypatch):
    login = "peer-node-deleted-login"

    class FakeDb:
        async def scalar(self, _query):
            return None

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    class InnerApp:
        def __init__(self):
            self.called = False

        async def __call__(self, _scope, _receive, _send):
            self.called = True

    async def run():
        auth.prime_generations({})
        cookie_session = {
            "principal": {"login": login},
            "auth_instance": auth._INSTANCE,
            "auth_generation": 3,
        }
        scope = {"type": "http", "path": "/api/admin/users", "session": cookie_session}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        protected = InnerApp()
        await auth.AuthVersionMiddleware(protected)(scope, receive, send)
        assert protected.called, "route still runs but the session was cleared below"
        assert cookie_session == {}
        assert login not in auth._generations

    settings = auth.get_settings().model_copy(update={"demo_no_db": False})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: FakeSession())
    try:
        asyncio.run(run())
    finally:
        auth._lookup_locks.pop(login, None)


def test_unknown_login_lookup_does_not_clobber_a_newer_local_revocation(monkeypatch):
    """A stale SELECT reply landing after a concurrent local revoke must not
    resurrect the revoked cookie's epoch (regression: lock only deduplicates
    concurrent lookups, it does not order a lookup against a write)."""
    login = "peer-node-race-login"
    started = asyncio.Event()
    resume = asyncio.Event()

    class FakeDb:
        async def scalar(self, _query):
            started.set()
            await resume.wait()
            return 0  # the epoch as it stood before the concurrent revoke below

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    async def run():
        auth.prime_generations({})
        task = asyncio.ensure_future(auth._resolve_unknown_login(login))
        await started.wait()  # the SELECT is in flight, holding the login's lock
        auth.invalidate_login(login, 1)  # a local logout/edit races the reply
        resume.set()
        result = await task
        assert result == 1, "the newer local revocation must win over the stale SELECT reply"
        assert auth._generations[login] == 1

    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: FakeSession())
    try:
        asyncio.run(run())
    finally:
        auth._generations.pop(login, None)
        auth._lookup_locks.pop(login, None)


def test_unknown_login_lookup_deduplicates_concurrent_requests_into_one_select(monkeypatch):
    login = "peer-node-concurrent-login"
    calls = []

    class FakeDb:
        async def scalar(self, _query):
            calls.append(1)
            await asyncio.sleep(0.01)  # widen the window for a racing second caller
            return 9

    class FakeSession:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *_args):
            return None

    async def run():
        auth.prime_generations({})
        results = await asyncio.gather(
            auth._resolve_unknown_login(login),
            auth._resolve_unknown_login(login),
            auth._resolve_unknown_login(login),
        )
        assert results == [9, 9, 9]
        assert calls == [1], "concurrent lookups for one login must issue a single SELECT"

    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: FakeSession())
    try:
        asyncio.run(run())
    finally:
        auth._generations.pop(login, None)
        auth._lookup_locks.pop(login, None)


def test_unknown_login_lookup_fails_closed_when_database_is_unreachable(monkeypatch):
    login = "peer-node-db-outage-login"

    class BrokenSession:
        async def __aenter__(self):
            raise OSError("database unavailable")

        async def __aexit__(self, *_args):
            return None

    class InnerApp:
        def __init__(self):
            self.called = False

        async def __call__(self, _scope, _receive, _send):
            self.called = True

    async def run():
        auth.prime_generations({})
        cookie_session = {
            "principal": {"login": login},
            "auth_instance": auth._INSTANCE,
            "auth_generation": 0,
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        http_messages = []

        async def http_send(message):
            http_messages.append(message)

        http_app = InnerApp()
        await auth.AuthVersionMiddleware(http_app)(
            {"type": "http", "path": "/api/admin/users", "session": dict(cookie_session)},
            receive, http_send,
        )
        assert not http_app.called
        assert http_messages[0]["status"] == 503

        ws_messages = []

        async def ws_send(message):
            ws_messages.append(message)

        ws_app = InnerApp()
        await auth.AuthVersionMiddleware(ws_app)(
            {"type": "websocket", "path": "/ws/control/x", "session": dict(cookie_session)},
            receive, ws_send,
        )
        assert not ws_app.called
        assert ws_messages[0] == {"type": "websocket.close", "code": 1013}

    settings = auth.get_settings().model_copy(update={"demo_no_db": False})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: BrokenSession())
    try:
        asyncio.run(run())
    finally:
        auth._lookup_locks.pop(login, None)


def test_auth_middleware_uses_fresh_generation_cache_without_per_request_database_query(monkeypatch):
    from app.config import get_settings

    class InnerApp:
        def __init__(self):
            self.called = False

        async def __call__(self, _scope, _receive, _send):
            self.called = True

    async def run():
        login = "cached-generation-user"
        auth.prime_generations({login: 6})
        principal = auth.Principal(
            login=login, full_name="Учётная запись", role=Role.INSTRUCTOR
        )
        scope = {"type": "http", "path": "/api/admin/users", "session": {
            "principal": principal.model_dump(mode="json"),
            "auth_instance": auth._INSTANCE,
            "auth_generation": 6,
        }}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        protected = InnerApp()
        await auth.AuthVersionMiddleware(protected)(scope, receive, send)
        assert protected.called, "a fresh, matching epoch should reach role-protected route auth"
        auth._generations.pop(login, None)

    settings = get_settings().model_copy(update={"demo_no_db": False})
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(
        auth, "get_sessionmaker",
        lambda: (_ for _ in ()).throw(AssertionError("middleware must use its synced cache")),
    )
    asyncio.run(run())


def test_stale_generation_sync_closes_existing_authenticated_websockets():
    class FakeWebSocket:
        def __init__(self):
            self.closed_with = None

        async def close(self, code, reason):
            self.closed_with = (code, reason)

    async def run():
        login = "stale-cache-socket-user"
        websocket = FakeWebSocket()
        sockets = auth._active_sockets.setdefault(
            login, weakref.WeakKeyDictionary()
        )
        sockets[websocket] = asyncio.get_running_loop()
        try:
            auth._close_unverified_sockets()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert websocket.closed_with == (
                1013, "Состояние доступа временно недоступно",
            )
        finally:
            auth._active_sockets.pop(login, None)

    asyncio.run(run())


def test_generation_watcher_fails_closed_after_database_sync_error(monkeypatch):
    class FakeWebSocket:
        def __init__(self):
            self.closed_with = None

        async def close(self, code, reason):
            self.closed_with = (code, reason)

    class StopWatcher(Exception):
        pass

    async def run():
        login = "sync-error-socket-user"
        websocket = FakeWebSocket()
        auth._active_sockets.setdefault(
            login, weakref.WeakKeyDictionary()
        )[websocket] = asyncio.get_running_loop()

        async def broken_sync():
            raise OSError("database unavailable")

        await_original_sleep = asyncio.sleep

        async def stop_after_iteration(_seconds):
            raise StopWatcher()

        monkeypatch.setattr(auth, "sync_generations", broken_sync)
        monkeypatch.setattr(auth.asyncio, "sleep", stop_after_iteration)
        monkeypatch.setattr(
            auth, "_generations_synced_at",
            auth.time.monotonic() - auth.AUTH_GENERATION_MAX_AGE_SECONDS - 1,
        )
        try:
            try:
                await auth.watch_generations()
            except StopWatcher:
                pass
            await await_original_sleep(0)
            await await_original_sleep(0)
            assert websocket.closed_with == (
                1013, "Состояние доступа временно недоступно",
            )
        finally:
            auth._active_sockets.pop(login, None)

    asyncio.run(run())


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
        password_hash="old", auth_provider="local", directory_subject=None,
        auth_version=0, created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_admin_patch_revokes_cookie_after_commit(monkeypatch):
    user = fake_user()
    db = FakeDb(user)
    calls = []
    monkeypatch.setattr(admin, "require", lambda _request, *_roles: auth.Principal(
        login="admin", full_name="Администратор", role=Role.ADMIN,
    ))
    monkeypatch.setattr(
        admin, "invalidate_login",
        lambda login, version=None: calls.append((login, version, db.commits)),
    )

    await admin.patch_user(user.id, admin.UserPatch(blocked=True), object(), db)
    assert user.blocked is True
    assert calls == [("victim", 1, 1)]
    assert db.added[-1].action == "user.update"
    assert db.added[-1].object_id == "victim"
    assert "заблокирован" in db.added[-1].detail


@pytest.mark.asyncio
async def test_admin_patch_never_revokes_or_reports_success_when_audit_commit_fails(monkeypatch):
    user = fake_user()

    class BrokenCommitDb(FakeDb):
        async def commit(self):
            raise RuntimeError("audit table unavailable")

    db = BrokenCommitDb(user)
    invalidations = []
    monkeypatch.setattr(admin, "require", lambda _request, *_roles: auth.Principal(
        login="admin", full_name="Администратор", role=Role.ADMIN,
    ))
    monkeypatch.setattr(admin, "invalidate_login", lambda *args: invalidations.append(args))

    with pytest.raises(RuntimeError, match="audit table unavailable"):
        await admin.patch_user(user.id, admin.UserPatch(blocked=True), object(), db)

    assert invalidations == [], "сессию отзываем только после атомарного commit"
    assert db.added[-1].action == "user.update"


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

    await admin.patch_user(user.id, admin.UserPatch(role=Role.TRAINEE), object(), db)
    assert user.role == "trainee"
    assert user.trainee_id is not None
    assert db.commits == 1
    assert db.added[-1].action == "user.update"
    assert db.added[-1].detail == "роль trainee"


@pytest.mark.asyncio
async def test_backup_runs_off_event_loop_and_failure_is_audited(monkeypatch):
    who = auth.Principal(login="admin", full_name="Администратор", role=Role.ADMIN)
    monkeypatch.setattr(admin, "require", lambda _request, *_roles: who)
    threadpool_calls = []
    audit_calls = []
    outcome_calls = []

    async def fake_threadpool(fn):
        threadpool_calls.append(fn)
        return fn()

    async def fake_audit_required(*args, **kwargs):
        audit_calls.append((args, kwargs))

    async def fake_audit(*args, **kwargs):
        outcome_calls.append((args, kwargs))

    monkeypatch.setattr(admin, "run_in_threadpool", fake_threadpool)
    monkeypatch.setattr(admin, "audit_required", fake_audit_required)
    monkeypatch.setattr(admin, "audit", fake_audit)
    monkeypatch.setattr(admin.backup_service, "create", lambda: {
        "name": "example.sql", "size_bytes": 1, "at": datetime.now(timezone.utc),
    })
    assert (await admin.make_backup(object())).name == "example.sql"
    assert threadpool_calls == [admin.backup_service.create]
    assert [item[0][2] for item in audit_calls] == [
        "backup.create.requested", "backup.create",
    ]

    def broken():
        raise admin.backup_service.BackupError("pg_dump failed")

    monkeypatch.setattr(admin.backup_service, "create", broken)
    with pytest.raises(HTTPException) as exc:
        await admin.make_backup(object())
    assert exc.value.status_code == 503
    assert outcome_calls[-1][0][2] == "backup.failed"


@pytest.mark.asyncio
async def test_backup_success_is_not_returned_when_audit_is_unavailable(monkeypatch):
    who = auth.Principal(login="admin", full_name="Администратор", role=Role.ADMIN)
    monkeypatch.setattr(admin, "require", lambda _request, *_roles: who)
    created = []

    async def fake_threadpool(fn):
        return fn()

    audit_actions = []

    async def fail_after_backup(*args, **_kwargs):
        audit_actions.append(args[2])
        if args[2] == "backup.create":
            raise HTTPException(status_code=503, detail="audit_unavailable")

    def create_backup():
        created.append("example.sql")
        return {"name": "example.sql", "size_bytes": 1, "at": datetime.now(timezone.utc)}

    monkeypatch.setattr(admin, "run_in_threadpool", fake_threadpool)
    monkeypatch.setattr(admin, "audit_required", fail_after_backup)
    monkeypatch.setattr(admin.backup_service, "create", create_backup)

    with pytest.raises(HTTPException) as exc:
        await admin.make_backup(object())

    assert exc.value.status_code == 503
    assert exc.value.detail == "audit_unavailable"
    assert audit_actions == ["backup.create.requested", "backup.create"]
    assert created == ["example.sql"]  # artifact exists; the response does not misreport audit success


def test_backup_error_redacts_database_credentials(monkeypatch):
    dsn = "postgresql://user:supersecret@localhost:5432/example"
    monkeypatch.setattr(admin, "get_settings", lambda: SimpleNamespace(database_url=dsn))
    detail = admin._safe_backup_error(admin.backup_service.BackupError(f"bad DSN: {dsn}"))
    assert "supersecret" not in detail
    assert dsn not in detail


def test_stale_generation_sync_keeps_synthetic_dev_socket():
    """`dev` не хранится в users, и middleware не сверяет его поколение.
    Закрывать его сокеты при недоступной БД — значит рвать занятие на
    стенде без базы, хотя HTTP того же входа продолжает работать."""
    class FakeWebSocket:
        closed_with = None

        async def close(self, code, reason):
            self.closed_with = (code, reason)

    async def run():
        websocket = FakeWebSocket()
        sockets = auth._active_sockets.setdefault("dev", weakref.WeakKeyDictionary())
        sockets[websocket] = asyncio.get_running_loop()
        try:
            auth._close_unverified_sockets()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert websocket.closed_with is None
        finally:
            sockets.pop(websocket, None)

    asyncio.run(run())
