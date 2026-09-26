from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from app.config import Settings
from app.db.models import AuditLog, Trainee, User
from app.directory import (
    DirectoryDenied,
    DirectoryUnavailable,
    _authenticate_sync,
    map_groups,
)
from app.domain.roles import Role


def test_directory_role_and_service_mappings_are_explicit_and_unambiguous():
    roles = {"CN=LCT Trainees,DC=training,DC=lan": "trainee"}
    services = {"CN=DDS 01,DC=training,DC=lan": "01"}
    assert map_groups(
        ["cn=dds 01,dc=training,dc=lan", "cn=lct trainees,dc=training,dc=lan"],
        roles,
        services,
    ) == (Role.TRAINEE, "01")
    with pytest.raises(DirectoryDenied):
        map_groups([], roles, services)
    with pytest.raises(DirectoryDenied):
        map_groups(
            [
                "CN=LCT Trainees,DC=training,DC=lan",
                "CN=Other Trainees,DC=training,DC=lan",
            ],
            {
                "CN=LCT Trainees,DC=training,DC=lan": "trainee",
                "CN=Other Trainees,DC=training,DC=lan": "instructor",
            },
            {},
        )
    with pytest.raises(DirectoryDenied):
        map_groups(
            [
                "CN=LCT Trainees,DC=training,DC=lan",
                "CN=DDS 01,DC=training,DC=lan",
                "CN=DDS 02,DC=training,DC=lan",
            ],
            roles,
            {
                "CN=DDS 01,DC=training,DC=lan": "01",
                "CN=DDS 02,DC=training,DC=lan": "02",
            },
        )


def test_directory_role_mapping_rejects_unknown_privilege_names():
    with pytest.raises(DirectoryUnavailable, match="invalid application role"):
        map_groups(
            ["CN=LCT Admins,DC=training,DC=lan"],
            {"CN=LCT Admins,DC=training,DC=lan": "superuser"},
            {},
        )


@pytest.mark.parametrize(
    "url, expected_tls",
    [
        ("ldaps://dc.training.lan:636", "ldaps"),
        ("ldap://dc.training.lan:389", "starttls"),
    ],
)
def test_directory_search_then_user_bind_uses_tls_and_escapes_login(
    monkeypatch, url, expected_tls
):
    import ldap3

    calls = []

    class Attribute:
        def __init__(self, value=None, values=None):
            self.value = value
            self.values = values or []

    entry = SimpleNamespace(
        entry_dn="CN=Training User,OU=People,DC=training,DC=lan",
        sAMAccountName=Attribute("Training.User"),
        displayName=Attribute("Учебный пользователь"),
        memberOf=Attribute(values=["CN=LCT Trainees,DC=training,DC=lan"]),
        objectGUID=Attribute(bytes(range(16))),
        entryUUID=Attribute(None),
    )

    class FakeServer:
        def __init__(self, host, **kwargs):
            calls.append(("server", host, kwargs))

    class FakeConnection:
        def __init__(self, server, **kwargs):
            calls.append(("connection", kwargs))
            self.entries = [entry]
            self.bound = False
            self.result = {"result": 0}

        def open(self):
            calls.append(("open",))
            return True

        def start_tls(self):
            calls.append(("start_tls",))
            return True

        def bind(self):
            calls.append(("service_bind",))
            self.bound = True
            return True

        def search(self, **kwargs):
            calls.append(("search", kwargs))
            return True

        def rebind(self, user, password):
            calls.append(("user_bind", user, password))
            self.bound = password == "correct-password"
            self.result = {"result": 0 if self.bound else 49}
            return self.bound

        def unbind(self):
            calls.append(("unbind",))

    monkeypatch.setattr(ldap3, "Server", FakeServer)
    monkeypatch.setattr(ldap3, "Connection", FakeConnection)
    monkeypatch.setattr(
        ldap3, "Tls", lambda **kwargs: calls.append(("tls", kwargs)) or object()
    )
    settings = Settings(
        ldap_enabled=True,
        ldap_url=url,
        ldap_base_dn="DC=training,DC=lan",
        ldap_bind_dn="CN=Reader,DC=training,DC=lan",
        ldap_bind_password="service-secret",
        ldap_role_groups={"CN=LCT Trainees,DC=training,DC=lan": "trainee"},
    )

    result = _authenticate_sync("Training.*(User", "correct-password", settings)
    assert result.login == "training.user"
    assert result.role is Role.TRAINEE
    assert result.subject == "03020100-0504-0706-0809-0a0b0c0d0e0f"
    search_call = next(call for call in calls if call[0] == "search")
    assert r"Training.\2a\28User" in search_call[1]["search_filter"]
    user_bind = next(call for call in calls if call[0] == "user_bind")
    assert user_bind[1] == entry.entry_dn
    if expected_tls == "starttls":
        assert calls.index(("start_tls",)) < calls.index(("service_bind",))
    else:
        server_call = next(call for call in calls if call[0] == "server")
        assert server_call[2]["use_ssl"] is True
        assert not any(call[0] == "start_tls" for call in calls)


def test_invalid_directory_password_is_denied(monkeypatch):
    import ldap3

    class Attribute:
        def __init__(self, value=None, values=None):
            self.value = value
            self.values = values or []

    entry = SimpleNamespace(
        entry_dn="CN=Training User,DC=training,DC=lan",
        sAMAccountName=Attribute("trainee"),
        displayName=Attribute("Trainee"),
        memberOf=Attribute(values=["CN=LCT Trainees,DC=training,DC=lan"]),
        objectGUID=Attribute("stable-guid"),
        entryUUID=Attribute(None),
    )

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.entries = [entry]
            self.bound = False
            self.result = {"result": 0}

        def open(self):
            return True

        def bind(self):
            self.bound = True
            return True

        def search(self, **kwargs):
            return True

        def rebind(self, user, password):
            self.bound = False
            self.result = {"result": 49}
            return False

        def unbind(self):
            pass

    monkeypatch.setattr(ldap3, "Server", lambda *args, **kwargs: object())
    monkeypatch.setattr(ldap3, "Connection", FakeConnection)
    monkeypatch.setattr(ldap3, "Tls", lambda **kwargs: object())
    settings = Settings(
        ldap_enabled=True,
        ldap_url="ldaps://dc.training.lan",
        ldap_base_dn="DC=training,DC=lan",
        ldap_bind_dn="CN=Reader,DC=training,DC=lan",
        ldap_bind_password="service-secret",
        ldap_role_groups={"CN=LCT Trainees,DC=training,DC=lan": "trainee"},
    )
    with pytest.raises(DirectoryDenied, match="invalid directory credentials"):
        _authenticate_sync("trainee", "wrong-password", settings)


def test_directory_account_without_stable_identifier_is_rejected(monkeypatch):
    import ldap3

    class Attribute:
        def __init__(self, value=None, values=None):
            self.value = value
            self.values = values or []

    entry = SimpleNamespace(
        entry_dn="CN=Training User,DC=training,DC=lan",
        sAMAccountName=Attribute("trainee"),
        displayName=Attribute("Trainee"),
        memberOf=Attribute(values=["CN=LCT Trainees,DC=training,DC=lan"]),
        objectGUID=Attribute(None),
        entryUUID=Attribute(None),
    )

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.entries = [entry]

        def open(self):
            return True

        def bind(self):
            return True

        def search(self, **kwargs):
            return True

        def unbind(self):
            pass

    monkeypatch.setattr(ldap3, "Server", lambda *args, **kwargs: object())
    monkeypatch.setattr(ldap3, "Connection", FakeConnection)
    monkeypatch.setattr(ldap3, "Tls", lambda **kwargs: object())
    settings = Settings(
        ldap_enabled=True,
        ldap_url="ldaps://dc.training.lan",
        ldap_base_dn="DC=training,DC=lan",
        ldap_bind_dn="CN=Reader,DC=training,DC=lan",
        ldap_bind_password="service-secret",
        ldap_role_groups={"CN=LCT Trainees,DC=training,DC=lan": "trainee"},
    )

    with pytest.raises(DirectoryUnavailable, match="objectGUID or entryUUID"):
        _authenticate_sync("trainee", "correct-password", settings)


@pytest.mark.asyncio
async def test_directory_account_is_jit_provisioned_and_role_sync_revokes_sessions(
    monkeypatch,
):
    from app.api import auth

    class FakeDb:
        user = None
        trainee = None
        audits = []

        async def scalar(self, _query):
            return self.user

        def add(self, row):
            if isinstance(row, Trainee):
                row.id = UUID("00000000-0000-4000-8000-000000000321")
                self.trainee = row
            elif isinstance(row, User):
                self.user = row
            elif isinstance(row, AuditLog):
                self.audits.append(row)

        async def get(self, model, _key):
            return self.trainee if model is Trainee else None

        async def flush(self):
            pass

        async def commit(self):
            pass

        async def rollback(self):
            pass

        async def refresh(self, _row):
            pass

    class Context:
        def __init__(self, db):
            self.db = db

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, *_args):
            return None

    fake_db = FakeDb()
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: Context(fake_db))
    identity = SimpleNamespace(
        login="trainee.one",
        full_name="Курсант Один",
        role=Role.TRAINEE,
        service="01",
        subject="stable-object-guid",
    )

    user = await auth._directory_account(identity)
    assert user.auth_provider == "ldap"
    assert user.directory_subject == "stable-object-guid"
    assert user.role == "trainee"
    assert user.service == "01"
    assert user.trainee_id == UUID("00000000-0000-4000-8000-000000000321")
    assert user.password_hash != "correct-password"
    assert [row.action for row in fake_db.audits] == ["user.provision.ldap"]

    auth._generations[user.login] = user.auth_version
    identity = SimpleNamespace(
        **{**vars(identity), "full_name": "Курсант Одинов", "service": "02"}
    )
    updated = await auth._directory_account(identity)
    assert updated.auth_version == 1
    assert updated.full_name == "Курсант Одинов"
    assert updated.service == "02"
    assert [row.action for row in fake_db.audits] == [
        "user.provision.ldap", "user.sync.ldap",
    ]
    assert auth._generations[user.login] == 0  # persistent value is loaded at login


@pytest.mark.asyncio
async def test_blocked_directory_account_is_returned_without_directory_sync(monkeypatch):
    from app.api import auth

    user = User(
        id=UUID("00000000-0000-4000-8000-000000000987"),
        login="trainee.one",
        full_name="Старое имя",
        role="trainee",
        service="01",
        trainee_id=None,
        blocked=True,
        password_hash="unused",
        auth_provider="ldap",
        directory_subject="stable-object-guid",
        auth_version=4,
    )

    class FakeDb:
        commits = 0

        async def scalar(self, _query):
            return user

        async def commit(self):
            self.commits += 1

    class Context:
        def __init__(self, db):
            self.db = db

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, *_args):
            return None

    fake_db = FakeDb()
    monkeypatch.setattr(auth, "get_sessionmaker", lambda: lambda: Context(fake_db))
    identity = SimpleNamespace(
        login="trainee.one",
        full_name="Новое имя из каталога",
        role=Role.ADMIN,
        service=None,
        subject="stable-object-guid",
    )

    returned = await auth._directory_account(identity)
    assert returned is user
    assert user.full_name == "Старое имя"
    assert user.role == "trainee"
    assert user.auth_version == 4
    assert fake_db.commits == 0
