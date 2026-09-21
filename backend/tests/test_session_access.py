"""HTTP-ссылки на занятие не дают курсанту чужую карточку или чек-лист."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request

from app.api.auth import Principal
from app.api.http import sessions
from app.domain.events import Exercise
from app.domain.roles import Role


def request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


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
