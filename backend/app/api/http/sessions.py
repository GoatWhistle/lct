"""Сессии: создание, состояние, история.

`group_id` и `mode` принимаются с первого дня — размечать накопленные сессии
задним числом не надо (docs/arch/CONTRACT.md#http-api).
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import audit, require
from app.config import get_settings
from app.db import repo
from app.db.base import get_session
from app.db.models import AuditLog, Score
from app.domain.events import Exercise, SessionMode, SessionReport
from app.scenarios import store
from app.scoring.report import build as build_report
from app.scoring.export import to_csv, to_pdf
from app.domain.roles import Role
from app.session.hub import hub
from app.voice.recording import recording_path

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
async def create(body: SessionCreate, request: Request, db: AsyncSession = Depends(get_session)) -> SessionOut:
    who = require(request, Role.INSTRUCTOR)
    try:
        group = await repo.ensure_group(db, body.group, owner_login=who.login) if body.group else None
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="group_not_found") from exc
    trainee = await repo.ensure_trainee(db, body.trainee, group) if body.trainee else None
    session = await repo.create_session(
        db,
        scenario_id=body.scenario_id,
        mode=body.mode.value,
        trainee_id=trainee.id if trainee else None,
        group_id=group.id if group else None,
        owner_login=who.login,
    )
    await audit(
        who.login,
        who.role.value,
        "session.create",
        str(session.id),
        f"scenario={session.scenario_id}; mode={session.mode}; attempt={session.attempt}",
    )
    return _out(session)


@router.get("/{session_id}", response_model=SessionOut)
async def read(session_id: UUID, request: Request, db: AsyncSession = Depends(get_session)) -> SessionOut:
    who = require(request)
    session = await repo.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if who.role is Role.TRAINEE and session.trainee_id != who.trainee_id:
        raise HTTPException(status_code=403, detail="not_your_session")
    if who.role is Role.INSTRUCTOR and session.owner_login != who.login:
        raise HTTPException(status_code=404, detail="session_not_found")
    return _out(session)


class ChecklistItemOut(BaseModel):
    id: str
    question: str


@router.get("/{session_id}/checklist", response_model=list[ChecklistItemOut])
async def checklist(session_id: UUID, request: Request) -> list[ChecklistItemOut]:
    """Чек-лист для самооценки — **только после конца звонка**.

    Во время звонка это содержимое подсказок: отдать его значит выдать
    в контрольном режиме то, чего там быть не должно. После звонка курсант
    по нему отмечает, что, по его мнению, пропустил.
    """
    who = require(request)
    state = hub.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if who.role is Role.TRAINEE and state.trainee_id != who.trainee_id:
        raise HTTPException(status_code=403, detail="not_your_session")
    if who.role is Role.INSTRUCTOR and state.owner_login != who.login:
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

    score_final: float = Field(ge=0, le=100)
    comment: str = Field(min_length=1, max_length=2000)

    @field_validator("comment")
    @classmethod
    def comment_must_not_be_blank(cls, comment: str) -> str:
        cleaned = comment.strip()
        if not cleaned:
            raise ValueError("обоснование корректировки обязательно")
        return cleaned


def _live(session_id: UUID):
    state = hub.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    scenario = state.scenario or store.get(state.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario_not_found")
    return state, scenario


async def _report_data(
    session_id: UUID, request: Request, db: AsyncSession,
) -> SessionReport:
    """Разбор сессии: метрики, отметки, эталонные вопросы, самооценка, пометки.

    Обучающийся открывает только свой разбор: ТЗ запрещает доступ к чужим
    результатам, а не только к чужим экранам. Проверка по `trainee_id`
    занятия, а не по номеру в ссылке.
    """
    who = require(request)
    try:
        state, scenario = _live(session_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        state = None
        scenario = None
    if state is not None and scenario is not None:
        if who.role is Role.INSTRUCTOR and state.owner_login != who.login:
            raise HTTPException(status_code=404, detail="session_not_found")
        if who.role is Role.TRAINEE and state.trainee_id != who.trainee_id:
            raise HTTPException(status_code=403, detail="not_your_session")
        if who.role is Role.TRAINEE and state.exercise is Exercise.CALL and not state.self_assessed:
            raise HTTPException(status_code=409, detail="self_assessment_required")
        if state.score is None:
            raise HTTPException(status_code=409, detail="score_not_ready")
        return build_report(session_id, state, scenario)

    session = await repo.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if who.role is Role.TRAINEE and session.trainee_id != who.trainee_id:
        raise HTTPException(status_code=403, detail="not_your_session")
    if who.role is Role.INSTRUCTOR and session.owner_login != who.login:
        raise HTTPException(status_code=404, detail="session_not_found")
    score = await db.scalar(select(Score).where(Score.session_id == session_id))
    if score is None:
        raise HTTPException(status_code=409, detail="score_not_ready")
    archived = (score.report or {}).get("full_report")
    if archived is None:
        raise HTTPException(status_code=409, detail="report_not_archived")
    data = SessionReport.model_validate(archived)
    if (who.role is Role.TRAINEE and data.reference_questions
            and data.self_assessment is None):
        raise HTTPException(status_code=409, detail="self_assessment_required")
    return data.model_copy(update={
        "score_auto": score.score_auto,
        "score_final": score.score_final,
        "overridden_by": score.overridden_by,
        "override_comment": score.override_comment,
    })


@router.get("/{session_id}/report", response_model=SessionReport)
async def report(
    session_id: UUID, request: Request, db: AsyncSession = Depends(get_session),
) -> SessionReport:
    return await _report_data(session_id, request, db)


@router.get("/{session_id}/report.csv")
async def report_csv(
    session_id: UUID, request: Request, db: AsyncSession = Depends(get_session),
) -> Response:
    """Те же права и готовность оценки, что у JSON-разбора."""
    data = await _report_data(session_id, request, db)
    return Response(
        content=to_csv(data), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="session-{session_id}-report.csv"'},
    )


@router.get("/{session_id}/report.pdf")
async def report_pdf(
    session_id: UUID, request: Request, db: AsyncSession = Depends(get_session),
) -> Response:
    """Печатный разбор; генерация полностью локальна."""
    data = await _report_data(session_id, request, db)
    try:
        content = to_pdf(data)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        content=content, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="session-{session_id}-report.pdf"'},
    )


@router.get("/{session_id}/recording.wav")
async def recording(session_id: UUID, request: Request, db: AsyncSession = Depends(get_session)):
    """Запись учебного звонка: преподавателю либо владельцу занятия."""
    who = require(request, Role.INSTRUCTOR, Role.TRAINEE)
    if get_settings().demo_no_db:
        state = hub.get(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        owner_id = state.trainee_id
        owner_login = state.owner_login
        ended = state.ended
    else:
        row = await repo.get_session(db, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        owner_id = row.trainee_id
        owner_login = row.owner_login
        ended = row.ended_at is not None
    if who.role is Role.TRAINEE and (owner_id is None or owner_id != who.trainee_id):
        raise HTTPException(status_code=403, detail="not_your_recording")
    if who.role is Role.INSTRUCTOR and owner_login != who.login:
        raise HTTPException(status_code=404, detail="recording_not_found")
    if not ended:
        raise HTTPException(status_code=409, detail="recording_not_ready")
    path = recording_path(session_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="recording_not_found")
    return FileResponse(
        path,
        media_type="audio/wav",
        filename=f"session-{session_id}-recording.wav",
    )


@router.patch("/{session_id}/report", response_model=SessionReport)
async def override(
    session_id: UUID,
    body: ScoreOverride,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> SessionReport:
    """Тренажёр готовит материал, преподаватель имеет последнее слово.

    Администратору сюда нельзя: ТЗ запрещает ему вмешиваться в оценки прямо.
    """
    who = require(request, Role.INSTRUCTOR)
    session = await repo.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    if session.owner_login != who.login:
        raise HTTPException(status_code=404, detail="session_not_found")
    state = hub.get(session_id)
    live_ready = state is not None and state.score is not None
    scenario = (state.scenario or store.get(state.scenario_id)) if live_ready else None
    if live_ready and scenario is None:
        raise HTTPException(status_code=409, detail="scenario_not_found")
    score = await db.scalar(select(Score).where(Score.session_id == session_id))
    if score is None:
        raise HTTPException(status_code=409, detail="score_not_ready")
    archived = (score.report or {}).get("full_report")
    if not live_ready and archived is None:
        raise HTTPException(status_code=409, detail="report_not_archived")

    # Persist score and audit entry in the same request transaction: the API
    # must not report success if either durable record failed to commit.
    score.score_final = body.score_final
    score.overridden_by = who.login
    score.override_comment = body.comment
    report_payload = dict(score.report or {})
    if archived is not None:
        archived_payload = dict(archived)
        archived_payload.update({
            "score_auto": score.score_auto,
            "score_final": body.score_final,
            "overridden_by": who.login,
            "override_comment": body.comment,
        })
        report_payload["full_report"] = archived_payload
        score.report = report_payload
    db.add(AuditLog(
        actor=who.login,
        role=who.role.value,
        action="score.override",
        object_id=str(session_id),
        detail=f"{score.score_auto} → {body.score_final}: {body.comment}"[:2000],
    ))
    await db.commit()

    if live_ready:
        assert state is not None and state.score is not None and scenario is not None
        state.score = {
            **state.score,
            "score_final": body.score_final,
            "overridden_by": who.login,
            "override_comment": body.comment,
        }
        state.score["full_report"] = {
            **state.score.get("full_report", {}),
            "score_auto": score.score_auto,
            "score_final": body.score_final,
            "overridden_by": who.login,
            "override_comment": body.comment,
        }
        result = build_report(session_id, state, scenario)
    else:
        # Historical/archived sessions no longer have a live object in the hub.
        # Keep score correction available from the instructor's report history.
        result = SessionReport.model_validate(archived).model_copy(update={
            "score_auto": score.score_auto,
            "score_final": body.score_final,
            "overridden_by": who.login,
            "override_comment": body.comment,
        })
    return result


@router.get("", response_model=list[SessionOut])
async def listing(
    request: Request,
    trainee: UUID | None = None,
    group: UUID | None = None,
    mode: SessionMode | None = None,
    since: datetime | None = Query(default=None, alias="from"),
    limit: int = 100,
    db: AsyncSession = Depends(get_session),
) -> list[SessionOut]:
    who = require(request)
    # Обучающийся видит только свою историю, что бы он ни передал в фильтре.
    if who.role is Role.TRAINEE:
        if who.trainee_id is None:
            raise HTTPException(status_code=403, detail="trainee_profile_required")
        trainee = who.trainee_id
    owner_login = who.login if who.role is Role.INSTRUCTOR else None
    rows = await repo.history(
        db,
        trainee_id=trainee,
        group_id=group,
        mode=mode.value if mode else None,
        owner_login=owner_login,
        since=since,
        limit=limit,
    )
    return [_out(row) for row in rows]
