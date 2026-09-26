"""Журнал сессий. Для полного прогона используй временную БД из `make test-db`;
если её нет — пропускаются, чтобы `make test` оставался запускаемым везде.
"""

from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth
from app.api.http import (
    groups as groups_api, materials as materials_api,
    scenario_submissions, scenarios as scenarios_api, sessions as sessions_api,
)
from app.api.ws import control as control_ws, station as station_ws
from app.api.http.scenarios import _hidden_scenario_ids
from app.config import get_settings
from app.db import repo
from app.db.models import (
    AuditLog, Group, LearningMaterial, MaterialAssignment, Scenario, ScenarioSubmission,
    Score, SelfAssessment, Session, Trainee, User, Utterance,
)
from app.db.repo import SessionNodeConflict, ensure_session
from app.domain.events import Exercise, SessionMode, Speaker
from app.domain.roles import Role
from app.main import LIBRARY, app
from app.scenarios import store
from app.session.checkpoint import load_state
from app.session.dds import build_card
from app.session.hub import hub
from app.session.pg_store import PostgresSessionStore
from app.session.store import (
    LessonRequest,
    ScoreCalculated,
    ScoreOverridden,
    SelfAssessed,
    SessionLeaseLost,
    UtteranceAppended,
)
from app.session.state import SessionState
from app.session.timers import SessionTimers


def _state(session_id, scenario, **fields) -> SessionState:
    return SessionState(
        session_id=session_id, scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level, mode=SessionMode.TRAINING, **fields,
    )


async def _open(store, scenario, session_id, trainee_name, trainee_id=None, owner_login=None):
    """Открыть занятие и вернуть его идентичность — как делает пульт."""
    seen = {}

    def build(identity):
        seen["identity"] = identity
        return _state(
            session_id, scenario, owner_login=owner_login, trainee_id=identity.trainee_id,
            attempt=identity.attempt, backend_fencing_epoch=identity.fencing_epoch,
        )

    state = await store.open(LessonRequest(
        session_id=session_id, scenario_id=scenario.id, mode="training",
        trainee_name=trainee_name, trainee_id=trainee_id, owner_login=owner_login,
        backend_node_id=None,
    ), build)
    return state, seen["identity"]


def _entry(text):
    from app.domain.events import Speaker, TranscriptEntry

    return TranscriptEntry(ref="u1", speaker=Speaker.CALLER, text=text,
                           at=datetime.now(timezone.utc))


@pytest.fixture
async def db():
    """Свой движок на каждый тест: глобальный в app.db.base кэшируется и
    привязывается к первому событийному циклу, а pytest даёт новый на каждый тест.

    Доступность проверяется подключением к порту, а не через engine.connect():
    у asyncpg нет таймаута по умолчанию, и когда Postgres действительно недоступен
    (Docker не поднят), connect() висит на TCP-таймауте ОС — минуты, а не секунды,
    и `make test` зависает вместо того, чтобы пропустить тест. Тот же баг был
    когда-то и в test_profile.py — здесь чинится тем же способом."""
    import socket
    from urllib.parse import urlparse

    url = urlparse(get_settings().database_url)
    try:
        socket.create_connection((url.hostname or "localhost", url.port or 5432), timeout=2).close()
    except OSError as exc:
        pytest.skip(f"Postgres недоступен ({exc}) — запусти `make test-db`")

    engine = create_async_engine(get_settings().database_url, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def scenario(db):
    row = Scenario(
        id=f"test-{uuid4().hex[:8]}",
        title="Тестовый",
        incident_type="fire",
        level="L1",
        topics=[],
        modes=["training"],
        body={},
    )
    db.add(row)
    await db.commit()
    scenario_id = row.id
    yield row
    await db.execute(delete(Session).where(Session.scenario_id == scenario_id))
    await db.execute(delete(Scenario).where(Scenario.id == scenario_id))
    await db.commit()


async def test_attempts_count_up(db, scenario):
    """Дельта попыток — измеримый цикл: разбор → повтор → дельта."""
    group = await repo.ensure_group(db, f"группа-{uuid4().hex[:6]}")
    trainee = await repo.ensure_trainee(db, f"курсант-{uuid4().hex[:6]}", group)

    first = await repo.create_session(
        db, scenario_id=scenario.id, mode="training", trainee_id=trainee.id, group_id=group.id
    )
    second = await repo.create_session(
        db, scenario_id=scenario.id, mode="exam", trainee_id=trainee.id, group_id=group.id
    )

    assert (first.attempt, second.attempt) == (1, 2)
    assert second.group_id == group.id, "группа размечается с первой миграции"
    await db.execute(delete(Trainee).where(Trainee.id == trainee.id))
    await db.execute(delete(Group).where(Group.id == group.id))
    await db.commit()


async def test_group_analytics_uses_scoped_persisted_scores(db, scenario, monkeypatch):
    """HTTP analytics handler aggregates real scores and excludes foreign ownership."""
    from starlette.requests import Request

    group = await repo.ensure_group(
        db, f"аналитика-{uuid4().hex[:8]}", owner_login="analytics-owner"
    )
    first = await repo.ensure_trainee(db, f"курсант-a-{uuid4().hex[:8]}", group)
    second = await repo.ensure_trainee(db, f"курсант-b-{uuid4().hex[:8]}", group)
    await repo.ensure_trainee(db, f"курсант-c-{uuid4().hex[:8]}", group)  # enrolled, no score

    persisted_sessions = []
    async def add_scored_attempt(trainee, score_value, codes, *, owner, explicit_group):
        session = await repo.create_session(
            db, scenario_id=scenario.id, mode="training", trainee_id=trainee.id,
            group_id=group.id if explicit_group else None, owner_login=owner,
        )
        session.ended_at = datetime.now(timezone.utc)
        db.add(Score(
            session_id=session.id, score_auto=score_value, score_final=score_value,
            report={"summary": {"codes": codes}},
        ))
        await db.commit()
        persisted_sessions.append(session.id)

    await add_scored_attempt(first, 40, {"E1": 2, "D1": 1}, owner="analytics-owner", explicit_group=True)
    await add_scored_attempt(first, 60, {"E1": 1}, owner="analytics-owner", explicit_group=True)
    # Legacy-compatible association through the current trainee group.
    await add_scored_attempt(second, 80, {"E1": 1}, owner="analytics-owner", explicit_group=False)
    await add_scored_attempt(second, 0, {"E5": 5}, owner="another-owner", explicit_group=True)

    monkeypatch.setattr(
        groups_api, "require",
        lambda _request, *_roles: SimpleNamespace(login="analytics-owner", role=Role.INSTRUCTOR),
    )
    request = Request({"type": "http", "session": {}})
    result = await groups_api.analytics(group.id, request, db)

    assert result.enrolled_trainees == 3
    assert result.active_trainees == 2
    assert result.scored_attempts == 3
    assert result.average_score == 60.0
    by_code = {error.code: error for error in result.errors}
    assert set(by_code) == {"E1", "D1"}
    assert (by_code["E1"].affected_trainees, by_code["E1"].occurrences, by_code["E1"].rate_percent) == (2, 4, 100.0)
    assert (by_code["D1"].affected_trainees, by_code["D1"].occurrences, by_code["D1"].rate_percent) == (1, 1, 50.0)
    assert all(error.recommendation for error in result.errors)

    monkeypatch.setattr(
        groups_api, "require",
        lambda _request, *_roles: SimpleNamespace(login="another-owner", role=Role.INSTRUCTOR),
    )
    with pytest.raises(HTTPException) as forbidden:
        await groups_api.analytics(group.id, request, db)
    assert forbidden.value.status_code == 404
    assert forbidden.value.detail == "group_not_found"

    await db.execute(delete(Session).where(Session.id.in_(persisted_sessions)))
    await db.execute(delete(Trainee).where(Trainee.group_id == group.id))
    await db.delete(group)
    await db.commit()


async def test_learning_materials_are_scoped_to_teacher_and_assigned_trainee(
    db, monkeypatch,
):
    """Material listing/mutation and learner access use durable tenant rows."""
    from starlette.requests import Request

    suffix = uuid4().hex[:8]
    group_a = await repo.ensure_group(db, f"materials-a-{suffix}", owner_login="teacher-a")
    group_b = await repo.ensure_group(db, f"materials-b-{suffix}", owner_login="teacher-b")
    trainee_a = await repo.ensure_trainee(db, f"materials-student-a-{suffix}", group_a)
    trainee_b = await repo.ensure_trainee(db, f"materials-student-b-{suffix}", group_b)
    material_a = LearningMaterial(
        title="Местная памятка A", description="", level="L1", kind="text",
        body="Текст для группы A", active=True, created_by="teacher-a",
    )
    material_b = LearningMaterial(
        title="Местная памятка B", description="", level="L1", kind="text",
        body="Текст для группы B", active=True, created_by="teacher-b",
    )
    db.add_all([material_a, material_b])
    await db.flush()
    assignment = MaterialAssignment(
        material_id=material_a.id, trainee_id=trainee_a.id, assigned_by="teacher-a",
    )
    db.add(assignment)
    await db.commit()

    request = Request({"type": "http", "session": {}})
    identity = {"login": "teacher-a", "role": Role.INSTRUCTOR, "trainee_id": None}
    monkeypatch.setattr(
        materials_api, "require",
        lambda _request, *_roles: SimpleNamespace(
            login=identity["login"], role=identity["role"], trainee_id=identity["trainee_id"],
            full_name="Test user",
        ),
    )

    teacher_a_materials = await materials_api.listing(request, db=db)
    assert [item.id for item in teacher_a_materials] == [material_a.id]
    assert teacher_a_materials[0].assignment_count == 1

    identity["login"] = "teacher-b"
    teacher_b_materials = await materials_api.listing(request, db=db)
    assert [item.id for item in teacher_b_materials] == [material_b.id]

    identity["login"] = "teacher-a"
    with pytest.raises(HTTPException) as edit_foreign:
        await materials_api.update(
            material_b.id, materials_api.MaterialPatch(title="Подмена"), request, db,
        )
    assert edit_foreign.value.status_code == 404
    with pytest.raises(HTTPException) as assign_foreign_trainee:
        await materials_api.assign(material_a.id, trainee_b.id, request, db)
    assert assign_foreign_trainee.value.status_code == 404
    with pytest.raises(HTTPException) as assign_foreign_group:
        await materials_api.assign_group(material_a.id, group_b.id, request, db)
    assert assign_foreign_group.value.status_code == 404

    identity.update(login="student-a", role=Role.TRAINEE, trainee_id=trainee_a.id)
    student_a_materials = await materials_api.listing(request, db=db)
    assert [item.id for item in student_a_materials] == [material_a.id]
    identity.update(login="student-b", trainee_id=trainee_b.id)
    assert await materials_api.listing(request, db=db) == []
    with pytest.raises(HTTPException) as complete_unassigned:
        await materials_api.complete(material_a.id, request, db)
    assert complete_unassigned.value.status_code == 403
    assert complete_unassigned.value.detail == "material_not_assigned"

    await db.execute(delete(MaterialAssignment).where(
        MaterialAssignment.material_id.in_([material_a.id, material_b.id])
    ))
    await db.execute(delete(LearningMaterial).where(
        LearningMaterial.id.in_([material_a.id, material_b.id])
    ))
    await db.execute(delete(Trainee).where(Trainee.id.in_([trainee_a.id, trainee_b.id])))
    await db.execute(delete(Group).where(Group.id.in_([group_a.id, group_b.id])))
    await db.commit()


async def test_archived_session_reports_are_owner_scoped_in_postgres(db, scenario, monkeypatch):
    """JSON/CSV/PDF report handlers deny persisted sessions owned by others."""
    from starlette.requests import Request

    trainee = await repo.ensure_trainee(db, f"report-owner-{uuid4().hex[:8]}")
    stranger = await repo.ensure_trainee(db, f"report-stranger-{uuid4().hex[:8]}")
    session = await repo.create_session(
        db, scenario_id=scenario.id, mode="training", trainee_id=trainee.id,
        owner_login="report-owner",
    )
    session.ended_at = datetime.now(timezone.utc)
    db.add(Score(
        session_id=session.id, score_auto=61, score_final=61,
        report={"full_report": {"private_marker": "archived report payload"}},
    ))
    await db.commit()
    request = Request({"type": "http", "session": {}})
    identity = {"login": "not-the-owner", "role": Role.INSTRUCTOR, "trainee_id": None}
    monkeypatch.setattr(
        sessions_api, "require",
        lambda _request, *_roles: SimpleNamespace(
            login=identity["login"], role=identity["role"], trainee_id=identity["trainee_id"],
        ),
    )

    for endpoint in (sessions_api.report, sessions_api.report_csv, sessions_api.report_pdf):
        with pytest.raises(HTTPException) as forbidden:
            await endpoint(session.id, request, db)
        assert forbidden.value.status_code == 404
        assert forbidden.value.detail == "session_not_found"

    identity.update(login="other-trainee", role=Role.TRAINEE, trainee_id=stranger.id)
    for endpoint in (sessions_api.report, sessions_api.report_csv, sessions_api.report_pdf):
        with pytest.raises(HTTPException) as forbidden:
            await endpoint(session.id, request, db)
        assert forbidden.value.status_code == 403
        assert forbidden.value.detail == "not_your_session"

    await db.delete(session)
    await db.execute(delete(Trainee).where(Trainee.id.in_([trainee.id, stranger.id])))
    await db.commit()


async def test_session_history_and_detail_are_owner_scoped_in_postgres(db, scenario, monkeypatch):
    """Session list/detail handlers apply durable teacher and learner ownership."""
    from starlette.requests import Request

    trainee_a = await repo.ensure_trainee(db, f"history-a-{uuid4().hex[:8]}")
    trainee_b = await repo.ensure_trainee(db, f"history-b-{uuid4().hex[:8]}")
    owned = await repo.create_session(
        db, scenario_id=scenario.id, mode="training", trainee_id=trainee_a.id,
        owner_login="history-teacher-a",
    )
    foreign = await repo.create_session(
        db, scenario_id=scenario.id, mode="exam", trainee_id=trainee_b.id,
        owner_login="history-teacher-b",
    )
    owned.ended_at = datetime.now(timezone.utc)
    foreign.ended_at = datetime.now(timezone.utc)
    await db.commit()

    request = Request({"type": "http", "session": {}})
    identity = {"login": "history-teacher-a", "role": Role.INSTRUCTOR, "trainee_id": None}
    monkeypatch.setattr(
        sessions_api, "require",
        lambda _request, *_roles: SimpleNamespace(
            login=identity["login"], role=identity["role"], trainee_id=identity["trainee_id"],
        ),
    )
    teacher_rows = await sessions_api.listing(request, since=None, db=db)
    assert [row.session_id for row in teacher_rows] == [owned.id]
    with pytest.raises(HTTPException) as instructor_read:
        await sessions_api.read(foreign.id, request, db)
    assert instructor_read.value.status_code == 404
    assert instructor_read.value.detail == "session_not_found"

    identity.update(login="learner-a", role=Role.TRAINEE, trainee_id=trainee_a.id)
    trainee_rows = await sessions_api.listing(request, since=None, db=db)
    assert [row.session_id for row in trainee_rows] == [owned.id]
    with pytest.raises(HTTPException) as trainee_read:
        await sessions_api.read(foreign.id, request, db)
    assert trainee_read.value.status_code == 403
    assert trainee_read.value.detail == "not_your_session"

    await db.execute(delete(Session).where(Session.id.in_([owned.id, foreign.id])))
    await db.execute(delete(Trainee).where(Trainee.id.in_([trainee_a.id, trainee_b.id])))
    await db.commit()


async def test_station_command_ids_survive_a_real_postgres_checkpoint(db, scenario):
    session = await repo.create_session(
        db, scenario_id=scenario.id, mode="training", session_id=uuid4(),
    )
    command_id = str(uuid4())
    state = _state(session.id, scenario, processed_station_commands=[command_id])
    pg = PostgresSessionStore(async_sessionmaker(db.bind, expire_on_commit=False))
    await pg.commit(state)

    observer_engine = create_async_engine(get_settings().database_url)
    try:
        async with observer_engine.connect() as observer:
            payload, checkpoint_at = (await observer.execute(
                select(Session.live_state, Session.checkpoint_at)
                .where(Session.id == session.id)
            )).one()
    finally:
        await observer_engine.dispose()

    assert command_id in payload["processed_station_commands"]
    restored = load_state(payload, checkpoint_at)
    assert command_id in restored.processed_station_commands
    await db.execute(delete(Session).where(Session.id == session.id))
    await db.commit()


async def test_security_audit_older_than_six_months_remains_queryable(db, monkeypatch):
    """The full-TZ minimum retention period must be observable on PostgreSQL."""
    from app.api.http import admin as admin_api

    marker = f"retention-{uuid4().hex[:16]}"
    old_at = datetime.now(timezone.utc) - timedelta(days=190)
    row = AuditLog(
        at=old_at, actor="retention-test", role="admin", action=marker,
        object_id=None, detail="retention integration check",
    )
    db.add(row)
    await db.commit()

    monkeypatch.setattr(
        admin_api, "require",
        lambda *_args, **_kwargs: auth.Principal(
            login="integration-admin", full_name="Интеграционный администратор", role=Role.ADMIN,
        ),
    )

    with TestClient(app) as client:
        login = client.post("/api/auth/dev-token")
        assert login.status_code == 200, login.text
        response = client.get("/api/admin/audit", params={"action": marker})
        assert response.status_code == 200, response.text
        records = response.json()
        assert len(records) == 1
        assert records[0]["action"] == marker
        assert records[0]["detail"] == "retention integration check"

    persisted = await db.get(AuditLog, row.id)
    assert persisted is not None
    assert persisted.at <= datetime.now(timezone.utc) - timedelta(days=180)
    await db.delete(persisted)
    await db.commit()


async def test_websocket_score_override_and_audit_commit_atomically(db, scenario):
    session = await repo.create_session(
        db, scenario_id=scenario.id, mode="training", owner_login="score-teacher"
    )
    score = Score(
        session_id=session.id, score_auto=70, score_final=70,
        report={"score_auto": 70, "full_report": {"score_auto": 70, "score_final": 70}},
    )
    db.add(score)
    await db.commit()
    pg = PostgresSessionStore(async_sessionmaker(db.bind, expire_on_commit=False))
    state = _state(session.id, scenario, owner_login="score-teacher")

    # Force the audit insert to fail after the score row is staged. PostgreSQL
    # must roll back both, and the commit must fail the whole operation.
    with pytest.raises(Exception):
        await pg.commit(state, [ScoreOverridden(85, "x" * 81, "instructor", "manual review")])
    await db.refresh(score)
    assert score.score_final == 70
    assert score.overridden_by is None
    assert await db.scalar(
        select(AuditLog.id).where(
            AuditLog.action == "score.override", AuditLog.object_id == str(session.id)
        )
    ) is None

    await pg.commit(state, [ScoreOverridden(85, "score-teacher", "instructor", "manual review")])
    await db.refresh(score)
    assert score.score_auto == 70
    assert score.score_final == 85
    assert score.overridden_by == "score-teacher"
    assert score.report["score_final"] == 85
    assert score.report["full_report"]["score_final"] == 85
    audit_row = await db.scalar(
        select(AuditLog).where(
            AuditLog.action == "score.override", AuditLog.object_id == str(session.id)
        )
    )
    assert audit_row is not None
    assert audit_row.actor == "score-teacher"
    assert "manual review" not in audit_row.detail
    await db.delete(audit_row)
    await db.commit()


async def test_initial_result_and_calculation_audit_commit_atomically(db, scenario):
    session = await repo.create_session(
        db, scenario_id=scenario.id, mode="training", owner_login="score-teacher"
    )
    pg = PostgresSessionStore(async_sessionmaker(db.bind, expire_on_commit=False))
    state = _state(session.id, scenario, owner_login="score-teacher")

    # Invalid JSON makes PostgreSQL reject the score transaction; no orphaned
    # calculation-audit row may remain (or vice versa).
    with pytest.raises(Exception):
        await pg.commit(state, [ScoreCalculated(71, {"bad": object()})])
    assert await db.scalar(
        select(Score.id).where(Score.session_id == session.id)
    ) is None
    assert await db.scalar(select(AuditLog.id).where(
        AuditLog.action == "score.calculate", AuditLog.object_id == str(session.id)
    )) is None

    await pg.commit(state, [ScoreCalculated(71, {"full_report": {"score_auto": 71}})])
    score = await db.scalar(select(Score).where(Score.session_id == session.id))
    audit_row = await db.scalar(select(AuditLog).where(
        AuditLog.action == "score.calculate", AuditLog.object_id == str(session.id)
    ))
    assert score is not None and score.score_final == 71
    assert audit_row is not None and audit_row.actor == "system"
    await db.delete(audit_row)
    await db.commit()


async def test_api_session_creation_and_audit_commit_atomically(db, scenario, monkeypatch):
    teacher = f"session-teacher-{uuid4().hex[:12]}"
    group_name = f"session-group-{uuid4().hex[:12]}"
    trainee_name = f"session-trainee-{uuid4().hex[:12]}"
    principal = auth.Principal(
        login=teacher, full_name="Интеграционный преподаватель", role=Role.INSTRUCTOR,
    )
    from app.api.http import sessions as sessions_api

    monkeypatch.setattr(sessions_api, "require", lambda *_args, **_kwargs: principal)
    result = await sessions_api.create(
        sessions_api.SessionCreate(
            scenario_id=scenario.id, mode="training",
            group=group_name, trainee=trainee_name,
        ),
        object(),
        db,
    )
    session_id = result.session_id
    persisted = await db.get(Session, session_id)
    assert persisted is not None and persisted.owner_login == teacher
    group = await db.get(Group, persisted.group_id)
    assert group is not None and group.owner_login == teacher
    trainee = await db.get(Trainee, persisted.trainee_id)
    assert trainee is not None and trainee.group_id == group.id
    audit_rows = (await db.scalars(select(AuditLog).where(
        AuditLog.object_id.in_([
            str(session_id), str(group.id), str(trainee.id),
        ]),
    ))).all()
    assert {row.action for row in audit_rows} == {
        "group.create", "trainee.profile.create", "session.create",
    }
    assert all(row.actor == teacher for row in audit_rows)
    await db.execute(delete(AuditLog).where(AuditLog.id.in_([row.id for row in audit_rows])))
    await db.execute(delete(Session).where(Session.id == session_id))
    await db.delete(trainee)
    await db.delete(group)
    await db.commit()


async def test_http_session_creation_cannot_claim_another_teachers_trainee(
    db, scenario, monkeypatch,
):
    teacher = f"session-owner-{uuid4().hex[:12]}"
    other_teacher = f"session-foreign-{uuid4().hex[:12]}"
    suffix = uuid4().hex[:12]
    foreign_group = Group(name=f"foreign-group-{suffix}", owner_login=other_teacher)
    db.add(foreign_group)
    await db.flush()
    foreign_trainee = Trainee(name=f"foreign-trainee-{suffix}", group_id=foreign_group.id)
    db.add(foreign_trainee)
    await db.commit()

    principal = auth.Principal(
        login=teacher, full_name="Преподаватель", role=Role.INSTRUCTOR,
    )
    monkeypatch.setattr(sessions_api, "require", lambda *_args, **_kwargs: principal)
    own_group_name = f"new-own-group-{suffix}"
    with pytest.raises(HTTPException) as denied:
        await sessions_api.create(
            sessions_api.SessionCreate(
                scenario_id=scenario.id, mode="training", group=own_group_name,
                trainee=foreign_trainee.name,
            ),
            object(),
            db,
        )
    assert denied.value.status_code == 404
    assert denied.value.detail == "trainee_not_found"
    # Group creation and audit/session inserts from this request must roll back too.
    assert await db.scalar(select(Group.id).where(Group.name == own_group_name)) is None
    assert await db.scalar(select(Session.id).where(Session.owner_login == teacher)) is None

    await db.delete(foreign_trainee)
    await db.delete(foreign_group)
    await db.commit()


async def test_websocket_lesson_start_cannot_claim_foreign_group_trainee(db, scenario):
    teacher = f"ws-session-owner-{uuid4().hex[:12]}"
    other_teacher = f"ws-session-foreign-{uuid4().hex[:12]}"
    suffix = uuid4().hex[:12]
    group = Group(name=f"ws-foreign-group-{suffix}", owner_login=other_teacher)
    db.add(group)
    await db.flush()
    trainee = Trainee(name=f"ws-foreign-trainee-{suffix}", group_id=group.id)
    db.add(trainee)
    await db.commit()

    session_id = uuid4()
    pg = PostgresSessionStore(async_sessionmaker(db.bind, expire_on_commit=False))
    with pytest.raises(PermissionError, match="другой учебной групп"):
        await _open(pg, scenario, session_id, trainee.name, trainee.id, owner_login=teacher)
    assert await db.get(Session, session_id) is None
    assert await db.scalar(select(AuditLog.id).where(
        AuditLog.action == "lesson.start", AuditLog.object_id == str(session_id)
    )) is None

    await db.delete(trainee)
    await db.delete(group)
    await db.commit()


async def test_websocket_lesson_start_audit_is_committed_with_session(db, scenario):
    session_id = uuid4()
    pg = PostgresSessionStore(async_sessionmaker(db.bind, expire_on_commit=False))
    _, identity = await _open(pg, scenario, session_id, "Курсант", owner_login="lesson-teacher")
    assert identity.attempt == 1 and identity.trainee_id is not None
    assert identity.service is None
    assert identity.fencing_epoch == 1
    persisted = await db.get(Session, session_id)
    assert persisted.live_state is not None, "первый снимок пишется вместе со строкой занятия"
    audit_row = await db.scalar(select(AuditLog).where(
        AuditLog.action == "lesson.start", AuditLog.object_id == str(session_id)
    ))
    assert persisted is not None and persisted.owner_login == "lesson-teacher"
    assert audit_row is not None and audit_row.actor == "lesson-teacher"
    await db.delete(audit_row)
    await db.commit()


async def test_store_commit_is_one_transaction_with_one_fencing_check(db, scenario):
    """Реплика, финиш и оценка — одна транзакция и один `(node_id, epoch)`-чек."""
    from sqlalchemy import event

    from app.session.store import LessonEnded

    session_id = uuid4()
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    opened = []

    def counting_maker():
        opened.append(True)
        return maker()

    pg = PostgresSessionStore(counting_maker, node_id="node-a")
    state, _ = await _open(pg, scenario, session_id, "Курсант", owner_login="lesson-teacher")
    statements = []

    def seen(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(db.bind.sync_engine, "before_cursor_execute", seen)
    opened.clear()
    try:
        entry = state.append(Speaker.CALLER, "адрес: Ленина, 14")
        state.ended_at = datetime.now(timezone.utc)
        await pg.commit(state, [
            UtteranceAppended(entry),
            LessonEnded(state.ended_at, "complete"),
            ScoreCalculated(71, {"score_auto": 71}),
        ])
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", seen)

    assert len(opened) == 1, "одна операция — одна транзакция"
    fence_checks = [sql for sql in statements
                    if sql.startswith("UPDATE sessions SET backend_lease_until")]
    assert len(fence_checks) == 1
    row = await db.get(Session, session_id)
    await db.refresh(row)
    assert row.end_reason == "complete" and row.live_state is None
    assert await db.scalar(select(Utterance.text).where(Utterance.session_id == session_id)) == (
        "адрес: Ленина, 14"
    )
    await db.execute(delete(AuditLog).where(AuditLog.object_id == str(session_id)))
    await db.execute(delete(Session).where(Session.id == session_id))
    await db.commit()


async def test_old_backend_fencing_epoch_cannot_append_transcript(db, scenario):
    session_id = uuid4()
    pg = PostgresSessionStore(async_sessionmaker(db.bind, expire_on_commit=False), node_id="node-a")
    state, identity = await _open(pg, scenario, session_id, "Курсант", owner_login="lesson-teacher")
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(backend_node_id="node-b", backend_fencing_epoch=identity.fencing_epoch + 1)
    )
    await db.commit()
    with pytest.raises(SessionLeaseLost):
        await pg.commit(state, [UtteranceAppended(_entry("проверка fencing"))])
    assert await db.scalar(select(Utterance.id).where(Utterance.session_id == session_id)) is None
    await db.execute(delete(AuditLog).where(AuditLog.object_id == str(session_id)))
    await db.execute(delete(Session).where(Session.id == session_id))
    await db.commit()


async def test_expired_backend_lease_is_atomically_claimed_and_restored(db, scenario):
    session_id = uuid4()
    old = PostgresSessionStore(async_sessionmaker(db.bind, expire_on_commit=False), node_id="node-a")
    snapshot, identity = await _open(old, scenario, session_id, "Курсант", owner_login="lease-teacher")
    snapshot.exercise = Exercise.DDS
    await old.commit(snapshot)
    row = await db.get(Session, session_id)
    assert row is not None
    row.backend_lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.commit()

    new = PostgresSessionStore(async_sessionmaker(db.bind, expire_on_commit=False), node_id="node-b")
    restored = await new.claim_expired(session_id)
    assert len(restored) == 1
    assert restored[0].session_id == session_id
    assert restored[0].backend_fencing_epoch == identity.fencing_epoch + 1
    persisted = await db.get(Session, session_id)
    assert persisted is not None
    await db.refresh(persisted)
    assert persisted.backend_node_id == "node-b"
    assert persisted.backend_fencing_epoch == identity.fencing_epoch + 1

    with pytest.raises(SessionLeaseLost):
        await old.commit(snapshot, [UtteranceAppended(_entry("stale owner"))])
    assert await db.scalar(select(Utterance.id).where(Utterance.session_id == session_id)) is None
    await db.execute(delete(AuditLog).where(AuditLog.object_id == str(session_id)))
    await db.execute(delete(Session).where(Session.id == session_id))
    await db.commit()


async def test_self_assessment_and_audit_commit_atomically(db, scenario):
    trainee = await repo.ensure_trainee(db, f"reflection-{uuid4().hex[:12]}")
    session = await repo.create_session(
        db, scenario_id=scenario.id, mode="training", trainee_id=trainee.id,
        owner_login="reflection-teacher",
    )
    pg = PostgresSessionStore(async_sessionmaker(db.bind, expire_on_commit=False))
    state = _state(session.id, scenario, trainee_id=trainee.id)

    class InvalidText:
        def __len__(self):
            return 12

    # Let the DB reject the staged assessment, and verify its audit rolls back too.
    with pytest.raises(Exception):
        await pg.commit(state, [SelfAssessed(["q_address"], InvalidText(), datetime.now(timezone.utc))])
    assert await db.scalar(select(SelfAssessment.id).where(
        SelfAssessment.session_id == session.id
    )) is None
    assert await db.scalar(select(AuditLog.id).where(
        AuditLog.action == "self_assessment.submit", AuditLog.object_id == str(session.id)
    )) is None

    await pg.commit(state, [SelfAssessed(
        ["q_address"], "адрес уточнил поздно", datetime.now(timezone.utc),
    )])
    assessment = await db.scalar(select(SelfAssessment).where(
        SelfAssessment.session_id == session.id
    ))
    audit_row = await db.scalar(select(AuditLog).where(
        AuditLog.action == "self_assessment.submit", AuditLog.object_id == str(session.id)
    ))
    assert assessment is not None and assessment.missed == ["q_address"]
    assert audit_row is not None and audit_row.actor == f"trainee:{trainee.id}"
    assert "адрес уточнил поздно" not in audit_row.detail
    await db.delete(audit_row)
    await db.delete(assessment)
    await db.delete(trainee)
    await db.commit()


async def test_peer_auth_version_change_closes_active_socket(db):
    import asyncio
    import weakref

    login = f"peer-{uuid4().hex[:16]}"
    user = User(
        login=login, password_hash="unused", full_name="Peer account",
        role="instructor", auth_version=0,
    )
    db.add(user)
    await db.commit()
    auth.prime_generations({login: 0})

    class FakeSocket:
        closed = False

        async def close(self, **_kwargs):
            self.closed = True

    socket = FakeSocket()
    auth._active_sockets[login] = weakref.WeakKeyDictionary({
        socket: asyncio.get_running_loop(),
    })
    user.auth_version = 1
    await db.commit()

    await auth.sync_generations()
    await asyncio.sleep(0.01)
    assert auth._generations[login] == 1
    assert socket.closed

    auth._active_sockets.pop(login, None)
    auth._generations.pop(login, None)
    await db.delete(user)
    await db.commit()


async def test_trainee_scenario_visibility_uses_real_group_owner_and_hides_other_instructors(db):
    suffix = uuid4().hex
    group = Group(name=f"visibility-{suffix}", owner_login=f"teacher-a-{suffix}")
    db.add(group)
    await db.flush()
    trainee = Trainee(name=f"visibility-student-{suffix}", group_id=group.id)
    own = Scenario(
        id=f"visibility-own-{suffix}", title="Своя", incident_type="fire", level="L1",
        topics=[], modes=["self"], owner_login=group.owner_login, body={},
    )
    other = Scenario(
        id=f"visibility-other-{suffix}", title="Чужая", incident_type="fire", level="L1",
        topics=[], modes=["self"], owner_login=f"teacher-b-{suffix}", body={},
    )
    db.add_all([trainee, own, other])
    await db.flush()

    who = SimpleNamespace(role=Role.TRAINEE, login="student", trainee_id=trainee.id)
    hidden = await _hidden_scenario_ids(db, who)
    assert own.id not in hidden
    assert other.id in hidden

    await db.execute(delete(Trainee).where(Trainee.id == trainee.id))
    await db.execute(delete(Scenario).where(Scenario.id.in_([own.id, other.id])))
    await db.execute(delete(Group).where(Group.id == group.id))
    await db.commit()


async def test_kio_submission_moderation_and_dds_card_persist_on_real_postgres(
    db, monkeypatch,
):
    """Exercise actual HTTP routes, PostgreSQL rows, and DDS template handoff."""
    from uuid import UUID

    from app.domain import ekp

    monkeypatch.setenv("DEMO_NO_DB", "false")
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    get_settings.cache_clear()
    store.load_from_disk(LIBRARY)
    source = store.get("t01-1-fire-container")
    assert source is not None
    suffix = uuid4().hex
    # The development websocket identity is `dev`; the owned group/scenario and
    # the actual control socket must share that owner for this integration path.
    teacher_login = "dev"
    group = Group(name=f"dds-e2e-{suffix}", owner_login=teacher_login)
    db.add(group)
    await db.flush()
    trainee = Trainee(name=f"student-{suffix}", group_id=group.id)
    db.add(trainee)
    await db.commit()
    trainee_id, group_id = trainee.id, group.id

    role = {"value": Role.TRAINEE}
    trainee_principal = auth.Principal(
        login=f"student-{suffix}", full_name="Интеграционный курсант",
        role=Role.TRAINEE, trainee_id=trainee_id,
    )
    instructor_principal = auth.Principal(
        login=teacher_login, full_name="Интеграционный преподаватель", role=Role.INSTRUCTOR,
    )
    monkeypatch.setattr(
        scenario_submissions, "require",
        lambda *_args, **_kwargs: trainee_principal
        if role["value"] is Role.TRAINEE else instructor_principal,
    )
    monkeypatch.setattr(
        scenarios_api, "require",
        lambda *_args, **_kwargs: instructor_principal,
    )
    monkeypatch.setattr(control_ws, "principal_of", lambda _ws: instructor_principal)
    monkeypatch.setattr(station_ws, "principal_of", lambda _ws: trainee_principal)

    scenario_id = None
    live_session_id = uuid4()
    try:
        with TestClient(app) as client:
            login = client.post("/api/auth/dev-token")
            assert login.status_code == 200, login.text
            kio = {
                "caller_name": "Учебный заявитель",
                "caller_contact": "+7 900 000-00-00",
                "address": "Москва, интеграционная улица, дом 10",
                "description": "Во дворе открыто горит мусорный контейнер.",
                "incident_group": ekp.incident(source.ground_truth.incident_code).group,
                "signs": source.signs,
                "incident_type": "fire",
                "dds": source.ground_truth.dds.value,
                "victims_count": 0,
                "fire": {"object_kind": "мусорный контейнер", "fire_nature": "открытое пламя"},
            }
            created = client.post("/api/scenario-submissions", json={
                "title": "PostgreSQL end-to-end KIO", "level": "L2", "kio": kio,
            })
            assert created.status_code == 201, created.text
            submission_id = UUID(created.json()["id"])
            pending = await db.get(ScenarioSubmission, submission_id)
            assert pending is not None and pending.status == "pending"
            assert pending.kio["address"] == kio["address"]

            role["value"] = Role.INSTRUCTOR
            visible = client.get("/api/scenario-submissions")
            assert visible.status_code == 200
            assert any(item["id"] == str(submission_id) for item in visible.json())
            approved = client.post(
                f"/api/scenario-submissions/{submission_id}/review",
                json={"decision": "approve", "comment": "Проверено."},
            )
            assert approved.status_code == 200, approved.text
            scenario_id = approved.json()["scenario_id"]
            await db.refresh(pending)
            assert pending.status == "approved" and pending.scenario_id == scenario_id
            published = await db.get(Scenario, scenario_id)
            assert published is not None and published.status == "published"
            assert published.owner_login == teacher_login
            assert published.body["student_card"]["address"] == kio["address"]

            # The instructor's catalog/detail views on a peer process must see
            # publication from the shared DB without waiting for its restart.
            store._library.pop(scenario_id, None)
            store._demo_scenario_owners.pop(scenario_id, None)
            catalog_response = client.get("/api/scenarios")
            assert catalog_response.status_code == 200, catalog_response.text
            catalog_item = next(
                item for item in catalog_response.json() if item["id"] == scenario_id
            )
            assert catalog_item["can_manage"] is True
            store._library.pop(scenario_id, None)
            detail_response = client.get(f"/api/scenarios/{scenario_id}")
            assert detail_response.status_code == 200, detail_response.text
            assert detail_response.json()["student_card"]["address"] == kio["address"]

            scenario = store.get(scenario_id)
            assert scenario is not None and scenario.student_card is not None
            card = build_card(scenario, 0, SessionTimers().limits)
            assert card.dispatched_card.address == kio["address"]
            assert card.dispatched_card.caller_name == kio["caller_name"]
            assert card.dispatched_card.fire.object_kind == "мусорный контейнер"

            # Simulate a peer backend with a cold process-local scenario cache:
            # scenario.start must resolve the approved row from shared Postgres.
            store._library.pop(scenario.id, None)
            store._demo_scenario_owners.pop(scenario.id, None)
            assert store.get(scenario.id) is None
            session_id = live_session_id
            with client.websocket_connect(f"/ws/control/{session_id}") as control:
                control.send_json({
                    "type": "scenario.start", "scenario_id": scenario.id,
                    "scenario_ids": [scenario.id], "trainee": trainee.name,
                    "trainee_id": str(trainee.id),
                    "dds_service": scenario.student_card.notify[0],
                    "mode": "training", "exercise": "dds",
                })
                deadline = time.monotonic() + 3
                while hub.get(session_id) is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert hub.get(session_id) is not None, "control socket did not start DDS session"
                with client.websocket_connect(f"/ws/station/{session_id}") as station:
                    def read_until(wanted):
                        received = []
                        for _ in range(30):
                            message = station.receive_json()
                            received.append(message["type"])
                            if message["type"] == "error":
                                raise AssertionError(
                                    f"station rejected: {message.get('code')}: "
                                    f"{message.get('message')}"
                                )
                            if message["type"] == wanted:
                                return message
                        raise AssertionError(f"missing {wanted}; received {received}")

                    received = read_until("card.received")
                    assert received["card"]["address"] == kio["address"]
                    state_event = read_until("station.state")
                    snapshot = state_event["snapshot"]
                    service = snapshot["managed_service"]
                    assert service == scenario.student_card.notify[0]
                    crew = snapshot["crew_options"][0]

                    def read_status(expected):
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            state = hub.get(session_id)
                            if state and state.desk.active and any(
                                mark.service == service and mark.status.value == expected
                                for mark in state.desk.active.status_log
                            ):
                                break
                            time.sleep(0.01)
                        else:
                            raise AssertionError(f"DDS status did not reach {expected}")
                        for _ in range(30):
                            message = station.receive_json()
                            if message["type"] == "error":
                                raise AssertionError(
                                    f"station rejected: {message.get('code')}: "
                                    f"{message.get('message')}"
                                )
                            if (message["type"] == "station.state"
                                    and message["snapshot"]["statuses"][service] == expected):
                                return message["snapshot"]
                        raise AssertionError(f"station did not report status {expected}")

                    station.send_json({"type": "crew.select", "crew": crew})
                    station.send_json({
                        "type": "card.status", "service": service,
                        "status": "accepted",
                        "comment": "Основание: подтверждение старшего группы.\nСведения: карточка принята.",
                    })
                    snapshot = read_status("accepted")
                    for status, comment in [
                        ("responding", "Основание: доклад старшего группы.\nСведения: выезд."),
                        ("arrived", "Основание: доклад старшего группы.\nСведения: прибытие."),
                        ("working", "Основание: доклад старшего группы.\nСведения: начало работ."),
                        ("completed", "Основание: доклад старшего группы.\nСведения: завершение работ."),
                    ]:
                        station.send_json({
                            "type": "card.status", "service": service,
                            "status": status, "comment": comment,
                        })
                        snapshot = read_status(status)
                    station.send_json({"type": "station.finish"})
                    read_until("score.ready")

            report = client.get(f"/api/sessions/{session_id}/report")
            assert report.status_code == 200, report.text
            body = report.json()
            assert body["exercise"] == "dds"
            assert body["card_results"][0]["scenario_id"] == scenario.id
            assert body["score_auto"] == 100
            assert body["score_final"] == 100
            assert any(
                action.get("type") == "card.status" and action.get("status") == "completed"
                for action in body["card_results"][0]["actions"]
            )
            hub.drop(session_id)
    finally:
        hub.drop(live_session_id)
        await db.execute(delete(Session).where(Session.id == live_session_id))
        if scenario_id is not None:
            store._library.pop(scenario_id, None)
            store._demo_scenario_owners.pop(scenario_id, None)
            await db.execute(delete(ScenarioSubmission).where(
                ScenarioSubmission.scenario_id == scenario_id
            ))
            await db.execute(delete(Scenario).where(Scenario.id == scenario_id))
        await db.execute(delete(Trainee).where(Trainee.id == trainee_id))
        await db.execute(delete(Group).where(Group.id == group_id))
        await db.commit()
        get_settings.cache_clear()


async def test_transcript_keeps_order_and_anchors(db, scenario):
    session = await repo.create_session(db, scenario_id=scenario.id, mode="training")
    at = datetime.now(timezone.utc)

    await repo.append_utterance(
        db, session_id=session.id, ref="u1", speaker="caller", text="Алло! Горим!", at=at, mood="panic"
    )
    await repo.append_utterance(
        db, session_id=session.id, ref="u2", speaker="operator", text="Назовите адрес", at=at
    )

    rows = await repo.transcript(db, session.id)
    assert [row.ref for row in rows] == ["u1", "u2"]
    assert rows[0].mood == "panic"


async def test_history_filters_by_mode(db, scenario):
    await repo.create_session(db, scenario_id=scenario.id, mode="training")
    await repo.create_session(db, scenario_id=scenario.id, mode="exam")

    exams = await repo.history(db, mode="exam")
    assert exams, "контрольные сессии не нашлись"
    assert all(row.mode == "exam" for row in exams)


async def test_dds_history_http_is_durable_and_owner_scoped(db, scenario, monkeypatch):
    own = await repo.create_session(
        db, scenario_id=scenario.id, mode="training", owner_login="dev"
    )
    foreign = await repo.create_session(
        db, scenario_id=scenario.id, mode="training", owner_login="other-instructor"
    )
    own.ended_at = datetime.now(timezone.utc)
    foreign.ended_at = datetime.now(timezone.utc)
    card_id = uuid4()
    db.add_all([
        Score(
            session_id=own.id, score_auto=75, score_final=80,
            report={"full_report": {"exercise": "dds", "card_results": [{
                "card_id": str(card_id), "scenario_id": scenario.id,
                "score_auto": 75, "reply_text": "Назначаю бригаду",
                "title": "Пожар в квартире", "address": "улица Тестовая, 7",
                "incident_type": "fire", "victims_count": 2,
                "received_at": datetime.now(timezone.utc).isoformat(),
                "managed_service": "01", "recipient_services": ["01", "03"],
            }]}},
        ),
        Score(
            session_id=foreign.id, score_auto=100, score_final=100,
            report={"full_report": {"exercise": "dds", "card_results": [{
                "card_id": str(uuid4()), "scenario_id": scenario.id,
                "score_auto": 100,
            }]}},
        ),
    ])
    await db.commit()

    try:
        async def no_catalog_restore(_db):
            return 0

        monkeypatch.setattr(store, "restore_published", no_catalog_restore)
        with TestClient(app) as client:
            token = client.post("/api/auth/dev-token")
            assert token.status_code == 200, token.text
            response = client.get("/api/sessions/dds-history")
            assert response.status_code == 200, response.text
            records = response.json()
            assert len(records) == 1
            assert records[0]["session_id"] == str(own.id)
            assert records[0]["card_id"] == str(card_id)
            assert records[0]["title"] == "Пожар в квартире"
            assert records[0]["address"] == "улица Тестовая, 7"
            assert records[0]["score_final"] == 80
        access = await db.scalar(select(AuditLog).where(
            AuditLog.action == "dds.history.read",
            AuditLog.actor == "dev",
            AuditLog.detail == "cards=1",
        ))
        assert access is not None
    finally:
        await db.execute(delete(Session).where(Session.id.in_([own.id, foreign.id])))
        await db.commit()


async def test_session_backend_owner_persists_and_rejects_another_node(db, scenario):
    """The PostgreSQL path must persist node affinity, not only the ORM unit path."""
    session = await repo.create_session(
        db,
        scenario_id=scenario.id,
        mode="training",
        owner_login="teacher-node-test",
        backend_node_id="node-a",
    )

    with pytest.raises(SessionNodeConflict):
        await ensure_session(
            db,
            session_id=session.id,
            scenario_id=scenario.id,
            mode="training",
            owner_login="teacher-node-test",
            backend_node_id="node-b",
        )

    persisted = await repo.get_session(db, session.id)
    assert persisted is not None
    assert persisted.backend_node_id == "node-a"


async def test_hints_are_logged(db, scenario):
    """Счёт подсказок — материал разбора, а не вычитаемое из баллов,
    но он обязан быть в журнале."""
    session = await repo.create_session(db, scenario_id=scenario.id, mode="training")
    await repo.record_hint(
        db,
        session_id=session.id,
        checklist_id="q_people",
        question="Есть ли люди в помещении?",
        at=datetime.now(timezone.utc),
    )
    count = await db.scalar(
        text("select count(*) from hint_uses where session_id = :sid").bindparams(sid=session.id)
    )
    assert count == 1
