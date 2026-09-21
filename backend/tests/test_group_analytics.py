"""Групповая сводка считает людей, а не число их повторных попыток."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import auth
from app.api.http import groups as group_api
from app.api.auth import Principal
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
async def test_group_creation_requires_staff_and_returns_new_group(monkeypatch):
    class FakeDb:
        def add(self, group):
            group.id = uuid4()

        async def commit(self):
            pass

    async def no_audit(*args):
        pass

    monkeypatch.setattr(group_api, "audit", no_audit)
    instructor = Principal(login="teacher", full_name="Преподаватель", role=Role.INSTRUCTOR)
    request = Request({"type": "http", "session": {}})
    auth._issue_session(request, instructor)
    created = await group_api.create(group_api.GroupCreate(name="  Группа 1  "), request, FakeDb())
    assert created.name == "Группа 1"
    assert created.id

    trainee = Principal(login="trainee", full_name="Курсант", role=Role.TRAINEE)
    forbidden = Request({"type": "http", "session": {}})
    auth._issue_session(forbidden, trainee)
    with pytest.raises(HTTPException) as exc:
        await group_api.create(group_api.GroupCreate(name="Чужая"), forbidden, FakeDb())
    assert exc.value.status_code == 403
