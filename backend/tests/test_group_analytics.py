"""Групповая сводка считает людей, а не число их повторных попыток."""

from uuid import uuid4
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import auth
from app.api.http import groups as group_api
from app.api.auth import Principal
from app.db.models import AuditLog
from app.domain.roles import Role
from app.scoring.group import ScoredAttempt, summarize


def test_group_errors_count_distinct_trainees_and_give_actions():
    first, second = uuid4(), uuid4()
    result = summarize([
        ScoredAttempt(first, 40.0, {"E1": 2, "D1": 1}),
        ScoredAttempt(first, 60.0, {"E1": 1}),
        ScoredAttempt(second, 80.0, {"E1": 1}),
    ], enrolled=3)

    assert result["active_trainees"] == 2
    assert result["scored_attempts"] == 3
    assert result["average_score"] == 60.0
    assert result["errors"][0]["code"] == "E1"
    assert result["errors"][0]["affected_trainees"] == 2
    assert result["errors"][0]["occurrences"] == 4
    assert result["errors"][0]["rate_percent"] == 100.0
    assert result["errors"][0]["recommendation"]


def test_group_without_scored_attempts_has_no_fake_recommendations():
    result = summarize([], enrolled=5)
    assert result == {
        "enrolled_trainees": 5,
        "active_trainees": 0,
        "scored_attempts": 0,
        "average_score": None,
        "errors": [],
    }


def test_unknown_codes_do_not_break_group_summary():
    result = summarize([ScoredAttempt(uuid4(), 75.0, {"unknown": 2, "E5": 1})], enrolled=1)
    assert [item["code"] for item in result["errors"]] == ["E5"]


@pytest.mark.asyncio
async def test_group_insight_fails_closed_if_audit_cannot_be_saved(monkeypatch):
    who = Principal(login="teacher", full_name="Преподаватель", role=Role.INSTRUCTOR)
    monkeypatch.setattr(group_api, "require", lambda _request, *_roles: who)

    async def fake_analytics(*_args, **_kwargs):
        return SimpleNamespace(scored_attempts=1, model_dump=lambda **_kwargs: {})

    async def fake_insight(_data):
        return {"summary": "Повторить уточнение адреса", "priorities": []}

    async def unavailable_audit(*_args, **_kwargs):
        raise HTTPException(status_code=503, detail="audit_unavailable")

    monkeypatch.setattr(group_api, "_analytics", fake_analytics)
    monkeypatch.setattr(group_api, "generate_group_insight", fake_insight)
    monkeypatch.setattr(group_api, "audit_required", unavailable_audit)

    with pytest.raises(HTTPException) as exc:
        await group_api.ai_insight(uuid4(), object(), object())

    assert exc.value.status_code == 503
    assert exc.value.detail == "audit_unavailable"


@pytest.mark.asyncio
async def test_group_creation_requires_staff_and_returns_new_group(monkeypatch):
    class FakeDb:
        def __init__(self):
            self.added = []
            self.commits = 0

        def add(self, row):
            self.added.append(row)

        async def commit(self):
            self.commits += 1
    instructor = Principal(login="teacher", full_name="Преподаватель", role=Role.INSTRUCTOR)
    request = Request({"type": "http", "session": {}})
    auth._issue_session(request, instructor)
    db = FakeDb()
    created = await group_api.create(group_api.GroupCreate(name="  Группа 1  "), request, db)
    assert created.name == "Группа 1"
    assert created.id
    assert db.commits == 1
    audit_row = next(row for row in db.added if isinstance(row, AuditLog))
    assert audit_row.action == "group.create" and audit_row.object_id == str(created.id)

    trainee = Principal(login="trainee", full_name="Курсант", role=Role.TRAINEE)
    forbidden = Request({"type": "http", "session": {}})
    auth._issue_session(forbidden, trainee)
    with pytest.raises(HTTPException) as exc:
        await group_api.create(group_api.GroupCreate(name="Чужая"), forbidden, FakeDb())
    assert exc.value.status_code == 403
