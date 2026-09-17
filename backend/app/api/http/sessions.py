"""Сессии: создание, состояние, история.

`group_id` и `mode` принимаются с первого дня — размечать накопленные сессии
задним числом не надо (docs/arch/CONTRACT.md#http-api).
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repo
from app.db.base import get_session
from app.domain.events import SessionMode, SessionReport
from app.scenarios import store
from app.scoring.report import build as build_report
from app.session.hub import hub

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionCreate(BaseModel):
    scenario_id: str
    mode: SessionMode
    trainee: str | None = None
    group: str | None = None


class SessionOut(BaseModel):
    session_id: UUID
    scenario_id: str
    mode: SessionMode
    attempt: int
    trainee_id: UUID | None = None
    group_id: UUID | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: str | None = None


def _out(session) -> SessionOut:
    return SessionOut(
        session_id=session.id,
        scenario_id=session.scenario_id,
        mode=session.mode,
        attempt=session.attempt,
        trainee_id=session.trainee_id,
        group_id=session.group_id,
        started_at=session.started_at,
        ended_at=session.ended_at,
        end_reason=session.end_reason,
    )


@router.post("", response_model=SessionOut, status_code=201)
async def create(body: SessionCreate, db: AsyncSession = Depends(get_session)) -> SessionOut:
    group = await repo.ensure_group(db, body.group) if body.group else None
    trainee = await repo.ensure_trainee(db, body.trainee, group) if body.trainee else None
    session = await repo.create_session(
        db,
        scenario_id=body.scenario_id,
        mode=body.mode.value,
        trainee_id=trainee.id if trainee else None,
        group_id=group.id if group else None,
    )
    return _out(session)


@router.get("/{session_id}", response_model=SessionOut)
async def read(session_id: UUID, db: AsyncSession = Depends(get_session)) -> SessionOut:
    session = await repo.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    return _out(session)


class ChecklistItemOut(BaseModel):
    id: str
    question: str


@router.get("/{session_id}/checklist", response_model=list[ChecklistItemOut])
async def checklist(session_id: UUID) -> list[ChecklistItemOut]:
    """Чек-лист для самооценки — **только после конца звонка**.

    Во время звонка это содержимое подсказок: отдать его значит выдать
    в контрольном режиме то, чего там быть не должно. После звонка курсант
    по нему отмечает, что, по его мнению, пропустил.
    """
    state = hub.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if not state.ended:
        raise HTTPException(status_code=409, detail="call_not_ended")
    scenario = state.scenario or store.get(state.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario_not_found")
    return [
        ChecklistItemOut(id=item.id, question=item.question)
        for item in scenario.checklist
        if item.question
    ]


class ScoreOverride(BaseModel):
    """Коррекция оценки преподавателем. Автооценка сохраняется рядом."""

    score_final: float
    comment: str = ""
    author: str = "преподаватель"


def _live(session_id: UUID):
    state = hub.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    scenario = state.scenario or store.get(state.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario_not_found")
    return state, scenario


@router.get("/{session_id}/report", response_model=SessionReport)
async def report(session_id: UUID) -> SessionReport:
    """Разбор сессии: метрики, отметки, эталонные вопросы, самооценка, пометки.

    Оценка курсанту открывается событием `score.ready` после самооценки; здесь
    прав нет — в прототипе нет входа, и точка доступна всем, у кого есть номер
    занятия (docs/arch/CONTRACT.md).
    """
    state, scenario = _live(session_id)
    if state.score is None:
        raise HTTPException(status_code=409, detail="score_not_ready")
    return build_report(session_id, state, scenario)


@router.patch("/{session_id}/report", response_model=SessionReport)
async def override(session_id: UUID, body: ScoreOverride) -> SessionReport:
    """Тренажёр готовит материал, преподаватель имеет последнее слово."""
    state, scenario = _live(session_id)
    if state.score is None:
        raise HTTPException(status_code=409, detail="score_not_ready")
    state.score = {
        **state.score,
        "score_final": body.score_final,
        "overridden_by": body.author,
        "override_comment": body.comment,
    }
    return build_report(session_id, state, scenario)


@router.get("", response_model=list[SessionOut])
async def listing(
    trainee: UUID | None = None,
    group: UUID | None = None,
    mode: SessionMode | None = None,
    since: datetime | None = Query(default=None, alias="from"),
    limit: int = 100,
    db: AsyncSession = Depends(get_session),
) -> list[SessionOut]:
    rows = await repo.history(
        db,
        trainee_id=trainee,
        group_id=group,
        mode=mode.value if mode else None,
        since=since,
        limit=limit,
    )
    return [_out(row) for row in rows]
