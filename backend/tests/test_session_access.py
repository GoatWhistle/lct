"""HTTP-ссылки на занятие не дают курсанту чужую карточку или чек-лист."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request

from app.db import repo
from app.db.models import Session, Utterance
from app.db.repo import SessionNodeConflict, ensure_session
from app.api.auth import Principal
from app.api.http import sessions
from app.api.ws import call as call_ws
from app.api.ws import observe as observe_ws
from app.api.ws import station as station_ws
from app.domain.events import Exercise, SessionMode, Speaker
from app.domain.roles import Role
from app.session.checkpoint import dump_state
from app.session.hub import SessionHub
from app.session.state import SessionState
from app.session.pg_store import PostgresSessionStore
from app.session.store import LessonRequest, UtteranceAppended


def request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


@pytest.mark.asyncio
async def test_store_commit_failure_does_not_log_user_text(caplog):
    private_text = "private caller address and medical detail"

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def add(self, _row):
            raise RuntimeError(private_text)

    state = SessionState(
        session_id=uuid4(), scenario_id="case", scenario_title="Сессия", level="L1",
        mode=SessionMode.TRAINING,
    )
    entry = state.append(Speaker.CALLER, private_text)
    with pytest.raises(RuntimeError):
        await PostgresSessionStore(lambda: FakeDb()).commit(state, [UtteranceAppended(entry)])
    assert private_text not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_trainee_cannot_read_foreign_session(monkeypatch):
    who = Principal(login="trainee", full_name="Курсант", role=Role.TRAINEE, trainee_id=uuid4())
    monkeypatch.setattr(sessions, "require", lambda _: who)

    async def row(_db, _session_id):
        return SimpleNamespace(trainee_id=uuid4())

    monkeypatch.setattr(sessions.repo, "get_session", row)
    with pytest.raises(HTTPException) as error:
        await sessions.read(uuid4(), request(), db=object())
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_instructor_cannot_read_foreign_session(monkeypatch):
    who = Principal(login="teacher-a", full_name="Преподаватель A", role=Role.INSTRUCTOR)
    monkeypatch.setattr(sessions, "require", lambda _: who)

    async def row(_db, _session_id):
        return SimpleNamespace(owner_login="teacher-b")

    monkeypatch.setattr(sessions.repo, "get_session", row)
    with pytest.raises(HTTPException) as error:
        await sessions.read(uuid4(), request(), db=object())
    assert error.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [sessions.report, sessions.report_csv, sessions.report_pdf])
async def test_instructor_cannot_read_or_export_foreign_archived_report(monkeypatch, endpoint):
    who = Principal(login="teacher-a", full_name="Преподаватель A", role=Role.INSTRUCTOR)
    monkeypatch.setattr(sessions, "require", lambda _request: who)
    monkeypatch.setattr(sessions.hub, "get", lambda _session_id: None)

    async def row(_db, _session_id):
        return SimpleNamespace(owner_login="teacher-b", trainee_id=uuid4())

    monkeypatch.setattr(sessions.repo, "get_session", row)
    with pytest.raises(HTTPException) as error:
        await endpoint(uuid4(), request(), db=object())
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_trainee_cannot_export_foreign_archived_report(monkeypatch):
    who = Principal(login="trainee", full_name="Курсант", role=Role.TRAINEE, trainee_id=uuid4())
    monkeypatch.setattr(sessions, "require", lambda _request: who)
    monkeypatch.setattr(sessions.hub, "get", lambda _session_id: None)

    async def row(_db, _session_id):
        return SimpleNamespace(owner_login="teacher-a", trainee_id=uuid4())

    monkeypatch.setattr(sessions.repo, "get_session", row)
    with pytest.raises(HTTPException) as error:
        await sessions.report_pdf(uuid4(), request(), db=object())
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_instructor_cannot_download_foreign_recording(monkeypatch):
    who = Principal(login="teacher-a", full_name="Преподаватель A", role=Role.INSTRUCTOR)
    monkeypatch.setattr(sessions, "require", lambda _request, *_roles: who)
    monkeypatch.setattr(sessions, "get_settings", lambda: SimpleNamespace(demo_no_db=False))

    async def row(_db, _session_id):
        return SimpleNamespace(
            owner_login="teacher-b", trainee_id=uuid4(), ended_at=None,
        )

    monkeypatch.setattr(sessions.repo, "get_session", row)
    with pytest.raises(HTTPException) as error:
        await sessions.recording(uuid4(), request(), db=object())
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_instructor_cannot_override_foreign_archived_score(monkeypatch):
    who = Principal(login="teacher-a", full_name="Преподаватель A", role=Role.INSTRUCTOR)
    monkeypatch.setattr(sessions, "require", lambda _request, *_roles: who)

    async def row(_db, _session_id):
        return SimpleNamespace(owner_login="teacher-b")

    monkeypatch.setattr(sessions.repo, "get_session", row)
    body = sessions.ScoreOverride(score_final=80, comment="Проверка")
    with pytest.raises(HTTPException) as error:
        await sessions.override(uuid4(), body, request(), db=object())
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_instructor_history_is_scoped_to_owner(monkeypatch):
    who = Principal(login="teacher-a", full_name="Преподаватель A", role=Role.INSTRUCTOR)
    monkeypatch.setattr(sessions, "require", lambda _: who)
    seen = {}

    async def history(_db, **filters):
        seen.update(filters)
        return []

    monkeypatch.setattr(sessions.repo, "history", history)
    await sessions.listing(request(), db=object())
    assert seen["owner_login"] == "teacher-a"


@pytest.mark.asyncio
async def test_demo_history_lists_only_owned_sessions_and_applies_filters(monkeypatch):
    owner_trainee = uuid4()
    other_trainee = uuid4()
    older = datetime(2026, 9, 20, tzinfo=UTC)
    newer = datetime(2026, 9, 25, tzinfo=UTC)
    demo_hub = SessionHub()
    for session_id, owner, trainee_id, mode, started, ended in [
        (uuid4(), "teacher-a", owner_trainee, SessionMode.TRAINING, older, newer),
        (uuid4(), "teacher-b", owner_trainee, SessionMode.TRAINING, newer, newer),
        (uuid4(), "teacher-a", other_trainee, SessionMode.EXAM, newer, newer),
    ]:
        demo_hub.register(SimpleNamespace(
            session_id=session_id, scenario_id="ticket-demo", mode=mode, attempt=1,
            trainee_id=trainee_id, owner_login=owner, lease_fenced=False,
            started_at=started, ended_at=ended, end_reason=None,
        ))
    who = Principal(login="teacher-a", full_name="Преподаватель A", role=Role.INSTRUCTOR)
    monkeypatch.setattr(sessions, "require", lambda _request: who)
    monkeypatch.setattr(sessions, "hub", demo_hub)
    monkeypatch.setattr(sessions, "get_settings", lambda: SimpleNamespace(demo_no_db=True))

    rows = await sessions.listing(
        request(), trainee=owner_trainee, mode=SessionMode.TRAINING,
        since=None, limit=100, db=None,
    )
    assert len(rows) == 1
    assert rows[0].trainee_id == owner_trainee
    assert rows[0].mode is SessionMode.TRAINING
    assert rows[0].ended_at == newer

    # Demo memory has no group membership records and must not ignore a group filter.
    assert await sessions.listing(request(), group=uuid4(), since=None, limit=100, db=None) == []

    trainee = Principal(
        login="learner", full_name="Курсант", role=Role.TRAINEE, trainee_id=owner_trainee,
    )
    monkeypatch.setattr(sessions, "require", lambda _request: trainee)
    trainee_rows = await sessions.listing(
        request(), trainee=other_trainee, since=None, limit=100, db=None,
    )
    assert trainee_rows
    assert {row.trainee_id for row in trainee_rows} == {owner_trainee}

    unlinked = Principal(login="unlinked", full_name="Без профиля", role=Role.TRAINEE)
    monkeypatch.setattr(sessions, "require", lambda _request: unlinked)
    with pytest.raises(HTTPException) as error:
        await sessions.listing(request(), since=None, limit=100, db=None)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_dds_history_returns_archived_cards_only_for_trainee(monkeypatch):
    trainee_id = uuid4()
    session_id = uuid4()
    card_id = uuid4()
    ended_at = datetime.now(UTC)
    who = Principal(
        login="trainee-a", full_name="Курсант A", role=Role.TRAINEE,
        trainee_id=trainee_id,
    )
    monkeypatch.setattr(sessions, "require", lambda *_args, **_kwargs: who)
    audit_events = []

    async def capture_audit(actor, role, action, object_id=None, detail=""):
        audit_events.append((actor, role, action, object_id, detail))

    monkeypatch.setattr(sessions, "audit_required", capture_audit)

    class Rows:
        def all(self):
            return [(
                SimpleNamespace(id=session_id, ended_at=ended_at),
                SimpleNamespace(score_final=82.5, report={"full_report": {
                    "exercise": "dds",
                    "card_results": [{
                        "card_id": str(card_id), "scenario_id": "fire-apartment",
                        "score_auto": 80, "reply_text": "Бригада направлена",
                        "title": "Пожар", "address": "улица Лесная, 4",
                        "incident_type": "fire", "victims_count": 1,
                        "managed_service": "01", "recipient_services": ["01", "03"],
                    }],
                }}),
            )]

    class Database:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return Rows()

    db = Database()
    result = await sessions.dds_history(request(), limit=200, db=db)

    sql = str(db.statement.compile(compile_kwargs={"literal_binds": True}))
    assert "sessions.trainee_id" in sql
    assert trainee_id.hex in sql
    assert len(result) == 1
    assert result[0].session_id == session_id
    assert result[0].card_id == card_id
    assert result[0].address == "улица Лесная, 4"
    assert result[0].score_final == 82.5
    assert audit_events == [("trainee-a", "trainee", "dds.history.read", None, "cards=1")]


@pytest.mark.asyncio
async def test_dds_history_rejects_admin_role(monkeypatch):
    who = Principal(login="admin", full_name="Администратор", role=Role.ADMIN)

    def require(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(sessions, "require", require)
    with pytest.raises(HTTPException) as error:
        await sessions.dds_history(request(), limit=200, db=None)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_live_registry_is_scoped_to_current_instructor(monkeypatch):
    who = Principal(login="teacher-a", full_name="Преподаватель A", role=Role.INSTRUCTOR)
    monkeypatch.setattr(sessions, "require", lambda _request, _role: who)
    seen = {}

    def active_sessions(owner_login):
        seen["owner_login"] = owner_login
        return []

    monkeypatch.setattr(sessions.hub, "active_sessions", active_sessions)
    assert await sessions.active(request(), db=None) == []
    assert seen["owner_login"] == "teacher-a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ws_module", "handler_name"),
    [
        (observe_ws, "observe"),
        (call_ws, "call"),
        (station_ws, "station"),
    ],
)
async def test_instructor_cannot_join_foreign_live_session(monkeypatch, ws_module, handler_name):
    session_id = uuid4()
    who = Principal(login="teacher-a", full_name="Преподаватель A", role=Role.INSTRUCTOR)
    state = SimpleNamespace(owner_login="teacher-b")
    monkeypatch.setattr(ws_module, "websocket_origin_allowed", lambda _ws: True)
    monkeypatch.setattr(ws_module, "principal_of", lambda _ws: who)
    monkeypatch.setattr(ws_module.hub, "get", lambda _session_id: state)

    class Socket:
        def __init__(self):
            self.accepted = False
            self.closed = False
            self.messages = []

        async def accept(self):
            self.accepted = True

        async def send_text(self, message):
            self.messages.append(message)

        async def close(self):
            self.closed = True

    socket = Socket()
    await getattr(ws_module, handler_name)(socket, session_id)

    assert socket.accepted and socket.closed
    assert len(socket.messages) == 1
    assert '"code":"session_not_found"' in socket.messages[0]
    assert "teacher-b" not in socket.messages[0]


@pytest.mark.asyncio
async def test_live_registry_includes_owned_checkpoints_from_other_nodes(monkeypatch):
    owner = "teacher-a"
    who = Principal(login=owner, full_name="Преподаватель A", role=Role.INSTRUCTOR)
    monkeypatch.setattr(sessions, "require", lambda _request, _role: who)
    monkeypatch.setattr(sessions.hub, "active_sessions", lambda _owner: [])
    state = SessionState(
        session_id=uuid4(),
        scenario_id="remote-case",
        scenario_title="Удалённое занятие",
        level="L2",
        mode=SessionMode.TRAINING,
        owner_login=owner,
        exercise=Exercise.DDS,
        trainee_name="Курсант",
    )
    state.started_at = datetime.now(UTC)
    row = SimpleNamespace(
        id=state.session_id,
        owner_login=owner,
        ended_at=None,
        live_state=dump_state(state),
        checkpoint_at=datetime.now(UTC),
    )

    class Rows:
        def all(self):
            return [row]

    class FakeDb:
        async def scalars(self, _query):
            return Rows()

    result = await sessions.active(request(), db=FakeDb())
    assert len(result) == 1
    assert result[0].session_id == state.session_id
    assert result[0].trainee_name == "Курсант"
    assert result[0].scenario_id == "remote-case"


@pytest.mark.asyncio
async def test_new_http_session_is_assigned_to_backend_node_at_creation(monkeypatch):
    who = Principal(login="teacher-a", full_name="Преподаватель A", role=Role.INSTRUCTOR)
    monkeypatch.setattr(sessions, "require", lambda _request, _role: who)
    monkeypatch.setattr(sessions, "get_settings", lambda: SimpleNamespace(backend_node_id="node-a"))
    monkeypatch.setattr(sessions.repo, "ensure_group", lambda *_args, **_kwargs: None)
    seen = {}

    async def create_session(_db, **kwargs):
        seen.update(kwargs)
        now = datetime.now(UTC)
        return SimpleNamespace(
            id=uuid4(), scenario_id=kwargs["scenario_id"], mode=kwargs["mode"],
            attempt=1, trainee_id=None, group_id=None,
            started_at=None, ended_at=None, end_reason=None, created_at=now,
        )

    async def audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sessions.repo, "create_session", create_session)
    monkeypatch.setattr(sessions, "audit", audit)
    result = await sessions.create(
        sessions.SessionCreate(scenario_id="case", mode=SessionMode.TRAINING),
        request(), db=object(),
    )
    assert result.scenario_id == "case"
    assert seen["backend_node_id"] == "node-a"


@pytest.mark.asyncio
async def test_trainee_cannot_read_foreign_checklist(monkeypatch):
    who = Principal(login="trainee", full_name="Курсант", role=Role.TRAINEE, trainee_id=uuid4())
    monkeypatch.setattr(sessions, "require", lambda _: who)
    monkeypatch.setattr(sessions.hub, "get", lambda _: SimpleNamespace(trainee_id=uuid4(), ended=True))
    with pytest.raises(HTTPException) as error:
        await sessions.checklist(uuid4(), request())
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_trainee_cannot_bypass_self_assessment_via_report(monkeypatch):
    trainee_id = uuid4()
    who = Principal(login="trainee", full_name="Курсант", role=Role.TRAINEE, trainee_id=trainee_id)
    monkeypatch.setattr(sessions, "require", lambda _: who)
    state = SimpleNamespace(
        trainee_id=trainee_id, exercise=Exercise.CALL, self_assessed=False,
        score={"score_auto": 100},
    )
    monkeypatch.setattr(sessions, "_live", lambda _: (state, object()))
    with pytest.raises(HTTPException) as error:
        await sessions.report(uuid4(), request())
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_trainee_without_profile_cannot_list_everyones_sessions(monkeypatch):
    who = Principal(login="unlinked", full_name="Курсант", role=Role.TRAINEE, trainee_id=None)
    monkeypatch.setattr(sessions, "require", lambda _: who)
    with pytest.raises(HTTPException) as error:
        await sessions.listing(request(), db=object())
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_existing_session_keeps_its_backend_owner():
    existing = SimpleNamespace(
        owner_login="teacher-a", backend_node_id="node-a"
    )

    class FakeDb:
        async def scalar(self, _query):
            return existing

    with pytest.raises(SessionNodeConflict):
        await ensure_session(
            FakeDb(),
            session_id=uuid4(),
            scenario_id="case",
            mode="training",
            owner_login="teacher-a",
            backend_node_id="node-b",
        )


@pytest.mark.asyncio
async def test_unassigned_existing_session_is_claimed_once():
    existing = SimpleNamespace(owner_login="teacher-a", backend_node_id=None)

    class FakeDb:
        commits = 0

        async def scalar(self, _query):
            return existing

        async def commit(self):
            self.commits += 1

    db = FakeDb()
    result = await ensure_session(
        db,
        session_id=uuid4(),
        scenario_id="case",
        mode="training",
        owner_login="teacher-a",
        backend_node_id="node-a",
    )
    assert result.backend_node_id == "node-a"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_store_opens_new_lesson_on_its_backend_node(monkeypatch):
    seen = {}
    trainee_id = uuid4()
    row = SimpleNamespace(
        id=uuid4(), attempt=3, trainee_id=trainee_id, backend_fencing_epoch=0,
        backend_lease_until=None, started_at=None, live_state=None, checkpoint_at=None,
    )

    async def ensure(_db, **kwargs):
        seen.update(kwargs)
        return row

    monkeypatch.setattr(repo, "ensure_session", ensure)

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _query):
            return "01"

        def add(self, item):
            added.append(item)

        async def commit(self):
            committed.append(True)

    added, committed, identities = [], [], []

    def build(identity):
        identities.append(identity)
        return SessionState(
            session_id=row.id, scenario_id="case", scenario_title="Сессия", level="L1",
            mode=SessionMode.TRAINING,
        )

    store = PostgresSessionStore(lambda: FakeDb(), node_id="node-a")
    await store.open(LessonRequest(
        session_id=row.id, scenario_id="case", mode="training", trainee_name="Курсант",
        trainee_id=trainee_id, owner_login="teacher-a", backend_node_id=None,
    ), build)
    identity = identities[0]
    assert (identity.attempt, identity.trainee_id, identity.service, identity.fencing_epoch) == (
        3, trainee_id, "01", 1,
    )
    assert seen["backend_node_id"] == "node-a"
    assert seen["commit"] is False, "строка, аудит и снимок коммитятся вместе"
    assert [item.action for item in added] == ["lesson.start"]
    assert row.live_state is not None and committed == [True]


@pytest.mark.asyncio
async def test_store_restores_checkpoint_owned_by_this_node():
    owner = "teacher-a"
    state = SessionState(
        session_id=uuid4(),
        scenario_id="case",
        scenario_title="Удалённая сессия",
        level="L1",
        mode=SessionMode.TRAINING,
        owner_login=owner,
        exercise=Exercise.DDS,
    )
    state.started_at = datetime.now(UTC)
    row = SimpleNamespace(
        id=state.session_id,
        owner_login=owner,
        backend_node_id="node-a",
        backend_fencing_epoch=0,
        backend_lease_until=None,
        live_state=dump_state(state),
        checkpoint_at=datetime.now(UTC),
    )

    class Rows:
        def __init__(self, values):
            self.values = values

        def all(self):
            return self.values

    class FakeDb:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return None

        async def commit(self):
            return None

        async def scalars(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            if entity is Utterance:
                return Rows([])
            if "backend_node_id IS NULL" in str(statement):
                return Rows([])
            return Rows([row])

    store = PostgresSessionStore(lambda: FakeDb(), node_id="node-a")
    restored = await store.restore_active()
    assert len(restored) == 1
    assert restored[0].session_id == state.session_id
    assert restored[0].owner_login == owner
