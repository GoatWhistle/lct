"""Сессии: создание, состояние, история.

`group_id` и `mode` принимаются с первого дня — размечать накопленные сессии
задним числом не надо (docs/arch/CONTRACT.md#http-api).
"""

import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import add_audit_entry, audit, audit_required, require
from app.config import get_settings
from app.db import repo
from app.db.base import get_session
from app.db.models import Group, Score, Session, Trainee
from app.domain.events import Exercise, SessionMode, SessionReport
from app.domain.roles import Role
from app.domain.statuses import SERVICE_STATUS_LABELS, StationSnapshot, current
from app.domain.timers import TimerCode
from app.scoring.export import to_csv, to_pdf
from app.scoring.report import build as build_report
from app.session.access import can_access
from app.session.checkpoint import load_state
from app.session.finish import override_score, scoring_scenario
from app.session.hub import hub
from app.session.store import ScoreOverridden, apply_score_override
from app.voice.recording import recording_path

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
log = logging.getLogger(__name__)


async def optional_session() -> AsyncIterator[AsyncSession | None]:
    if get_settings().demo_no_db:
        yield None
    else:
        async for db in get_session():
            yield db


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


class DdsHistoryOut(BaseModel):
    """Одна завершённая карточка из отчёта занятия; только в границах владельца."""

    session_id: UUID
    ended_at: datetime
    card_id: UUID
    scenario_id: str
    score_auto: float
    score_final: float
    reply_text: str = ""
    title: str | None = None
    address: str | None = None
    description: str | None = None
    incident_type: str | None = None
    victims_count: int | None = None
    received_at: datetime | None = None
    managed_service: str | None = None
    recipient_services: list[str] = []


class ActiveSessionOut(BaseModel):
    session_id: UUID
    trainee_name: str | None
    scenario_id: str
    scenario_title: str
    mode: SessionMode
    exercise: Exercise
    started_at: datetime | None
    elapsed_seconds: int
    dds_card_total: int
    dds_open_cards: int
    dds_overdue_cards: int
    dds_work_overdue_cards: int
    dds_statuses: dict[str, str]
    dds_snapshot: StationSnapshot | None = None


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


@router.get("/dds-history", response_model=list[DdsHistoryOut])
async def dds_history(
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession | None = Depends(optional_session),
) -> list[DdsHistoryOut]:
    """Durable completed-card registry, limited to the current trainee/instructor."""
    who = require(request, Role.TRAINEE, Role.INSTRUCTOR)
    if db is None:
        await audit_required(
            who.login, who.role.value, "dds.history.read", detail="cards=0"
        )
        return []
    statement = (
        select(Session, Score)
        .join(Score, Score.session_id == Session.id)
        .where(Session.ended_at.is_not(None))
        .order_by(Session.ended_at.desc())
        .limit(limit)
    )
    if who.role is Role.TRAINEE:
        if who.trainee_id is None:
            raise HTTPException(status_code=403, detail="trainee_profile_required")
        statement = statement.where(Session.trainee_id == who.trainee_id)
    else:
        statement = statement.where(Session.owner_login == who.login)

    rows = (await db.execute(statement)).all()
    result: list[DdsHistoryOut] = []
    for session, score in rows:
        report = score.report or {}
        full_report = report.get("full_report") or report
        if full_report.get("exercise") != Exercise.DDS.value:
            continue
        for card in full_report.get("card_results", []):
            try:
                result.append(DdsHistoryOut(
                    session_id=session.id,
                    ended_at=session.ended_at,
                    card_id=card["card_id"],
                    scenario_id=card["scenario_id"],
                    score_auto=card["score_auto"],
                    score_final=score.score_final,
                    reply_text=card.get("reply_text", ""),
                    title=card.get("title"),
                    address=card.get("address"),
                    description=card.get("description"),
                    incident_type=card.get("incident_type"),
                    victims_count=card.get("victims_count"),
                    received_at=card.get("received_at"),
                    managed_service=card.get("managed_service"),
                    recipient_services=card.get("recipient_services", []),
                ))
            except (KeyError, TypeError, ValueError):
                log.warning("Пропущена некорректная карточка ДДС в отчёте сессии %s", session.id)
            if len(result) >= limit:
                await audit_required(
                    who.login, who.role.value, "dds.history.read",
                    detail=f"cards={len(result)}",
                )
                return result
    await audit_required(
        who.login, who.role.value, "dds.history.read", detail=f"cards={len(result)}"
    )
    return result


@router.get("/active", response_model=list[ActiveSessionOut])
async def active(
    request: Request,
    db: AsyncSession | None = Depends(optional_session),
) -> list[ActiveSessionOut]:
    """Компактный live-реестр сессий преподавателя; детали остаются в /ws/observe."""
    who = require(request, Role.INSTRUCTOR)
    now = datetime.now(UTC)
    result: list[ActiveSessionOut] = []
    states = {
        state.session_id: state
        for state in hub.active_sessions(who.login)
    }
    if db is not None:
        rows = (
            await db.scalars(
                select(Session).where(
                    Session.owner_login == who.login,
                    Session.ended_at.is_(None),
                    Session.live_state.is_not(None),
                    Session.checkpoint_at.is_not(None),
                )
            )
        ).all()
        for row in rows:
            local = hub.get(row.id)
            if local is not None:
                if local.owner_login == who.login and not local.ended:
                    states[row.id] = local
                else:
                    states.pop(row.id, None)
                continue
            try:
                state = load_state(row.live_state, row.checkpoint_at)
            except Exception as exc:  # noqa: BLE001 — один плохой checkpoint не ломает весь реестр
                log.error("Не удалось прочитать checkpoint сессии %s (%s)",
                          row.id, type(exc).__name__)
                continue
            state.owner_login = row.owner_login
            if not state.ended:
                states[state.session_id] = state

    for state in states.values():
        elapsed = (max(0, int((now - state.started_at).total_seconds()))
                   if state.started_at else 0)
        station = state.station_snapshot() if state.exercise is Exercise.DDS else None
        queue = station.queue_cards if station else []
        card = state.desk.active
        status_log = card.status_log if card is not None else []
        managed_services = state.managed_services()
        latest_statuses = {
            service: SERVICE_STATUS_LABELS[current(status_log, service)]
            for service in managed_services
            if (status_log or state.exercise is Exercise.DDS)
        }
        result.append(ActiveSessionOut(
            session_id=state.session_id,
            trainee_name=state.trainee_name,
            scenario_id=state.scenario_id,
            scenario_title=state.scenario_title,
            mode=state.mode,
            exercise=state.exercise,
            started_at=state.started_at,
            elapsed_seconds=elapsed,
            dds_card_total=len(state.desk.scenarios),
            dds_open_cards=len(queue),
            dds_overdue_cards=sum(
                not card.timer_stopped and card.elapsed_ms > card.limit_ms for card in queue
            ),
            dds_work_overdue_cards=sum(
                (timer := card.timers.timers.get(TimerCode.DDS_WORK)) is not None
                and timer.started_at is not None
                and not timer.stopped
                and timer.current_ms(time.monotonic()) > card.timers.limits[TimerCode.DDS_WORK]
                for card in state.desk.cards.values()
            ),
            dds_statuses=latest_statuses,
            dds_snapshot=station,
        ))
    return result


@router.post("", response_model=SessionOut, status_code=201)
async def create(body: SessionCreate, request: Request, db: AsyncSession = Depends(get_session)) -> SessionOut:
    who = require(request, Role.INSTRUCTOR)
    group_created = False
    try:
        if body.group:
            group = await db.scalar(select(Group).where(Group.name == body.group))
            group_created = group is None
            group = await repo.ensure_group(
                db, body.group, owner_login=who.login, commit=False
            )
        else:
            group = None
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="group_not_found") from exc
    trainee_created = False
    if body.trainee:
        trainee = await db.scalar(select(Trainee).where(Trainee.name == body.trainee))
        trainee_created = trainee is None
        try:
            trainee = await repo.ensure_trainee(
                db, body.trainee, group, owner_login=who.login, commit=False
            )
        except PermissionError as exc:
            # A group created earlier in this same request must not be left
            # behind when the selected learner is outside this instructor's scope.
            await db.rollback()
            raise HTTPException(status_code=404, detail="trainee_not_found") from exc
    else:
        trainee = None

    def audit_creation(transaction, row):
        if group_created and group is not None:
            add_audit_entry(
                transaction, who.login, who.role.value,
                "group.create", str(group.id), group.name,
            )
        if trainee_created and trainee is not None:
            add_audit_entry(
                transaction, who.login, who.role.value,
                "trainee.profile.create", str(trainee.id),
            )
        add_audit_entry(
            transaction,
            who.login,
            who.role.value,
            "session.create",
            str(row.id),
            f"scenario={row.scenario_id}; mode={row.mode}; attempt={row.attempt}",
        )

    session = await repo.create_session(
        db,
        scenario_id=body.scenario_id,
        mode=body.mode.value,
        trainee_id=trainee.id if trainee else None,
        group_id=group.id if group else None,
        owner_login=who.login,
        backend_node_id=get_settings().backend_node_id,
        before_commit=audit_creation,
    )
    return _out(session)


def _require_access(who, lesson, what: str = "session") -> None:
    """Отказ по правилу `can_access`: курсанту — 403, чужому преподавателю
    занятие «не существует» — 404, владелец не раскрывается."""
    if can_access(who, lesson):
        return
    if who.role is Role.TRAINEE:
        raise HTTPException(status_code=403, detail=f"not_your_{what}")
    raise HTTPException(status_code=404, detail=f"{what}_not_found")


@router.get("/{session_id}", response_model=SessionOut)
async def read(session_id: UUID, request: Request, db: AsyncSession = Depends(get_session)) -> SessionOut:
    who = require(request)
    session = await repo.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    _require_access(who, session)
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
    _require_access(who, state)
    if not state.ended:
        raise HTTPException(status_code=409, detail="call_not_ended")
    scenario = scoring_scenario(state)
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
    scenario = scoring_scenario(state)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario_not_found")
    return state, scenario


async def _report_data(
    session_id: UUID, request: Request, db: AsyncSession | None,
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
        _require_access(who, state)
        if who.role is Role.TRAINEE and state.exercise is Exercise.CALL and not state.self_assessed:
            raise HTTPException(status_code=409, detail="self_assessment_required")
        if state.score is None:
            raise HTTPException(status_code=409, detail="score_not_ready")
        if hub.store.persistent and isinstance(db, AsyncSession):
            persisted_session = await db.scalar(
                select(Session.id).where(Session.id == session_id)
            )
            if (persisted_session is not None and await db.scalar(
                select(Score.session_id).where(Score.session_id == session_id)
            ) is None):
                # Live state is populated just before the store commit finishes.
                # Do not expose a report that looks ready but cannot yet be corrected
                # or retrieved after restart.
                raise HTTPException(status_code=409, detail="score_not_ready")
        return build_report(session_id, state, scenario)

    if db is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    session = await repo.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    _require_access(who, session)
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
    session_id: UUID, request: Request, db: AsyncSession | None = Depends(optional_session),
) -> SessionReport:
    data = await _report_data(session_id, request, db)
    who = require(request)
    await audit_required(who.login, who.role.value, "report.read", str(session_id))
    return data


@router.get("/{session_id}/report.csv")
async def report_csv(
    session_id: UUID, request: Request, db: AsyncSession | None = Depends(optional_session),
) -> Response:
    """Те же права и готовность оценки, что у JSON-разбора."""
    data = await _report_data(session_id, request, db)
    content = to_csv(data)
    who = require(request)
    await audit_required(who.login, who.role.value, "report.export.csv", str(session_id))
    return Response(
        content=content, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="session-{session_id}-report.csv"'},
    )


@router.get("/{session_id}/report.pdf")
async def report_pdf(
    session_id: UUID, request: Request, db: AsyncSession | None = Depends(optional_session),
) -> Response:
    """Печатный разбор; генерация полностью локальна."""
    data = await _report_data(session_id, request, db)
    try:
        content = to_pdf(data)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    who = require(request)
    await audit_required(who.login, who.role.value, "report.export.pdf", str(session_id))
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
        lesson, ended = state, state.ended
    else:
        row = await repo.get_session(db, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="session_not_found")
        lesson, ended = row, row.ended_at is not None
    _require_access(who, lesson, "recording")
    if not ended:
        raise HTTPException(status_code=409, detail="recording_not_ready")
    path = recording_path(session_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="recording_not_found")
    await audit_required(who.login, who.role.value, "recording.read", str(session_id))
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
    db: AsyncSession | None = Depends(optional_session),
) -> SessionReport:
    """Тренажёр готовит материал, преподаватель имеет последнее слово.

    Администратору сюда нельзя: ТЗ запрещает ему вмешиваться в оценки прямо.
    Правка идёт тем же путём, что команда `score.override` с пульта.
    """
    who = require(request, Role.INSTRUCTOR)
    correction = ScoreOverridden(
        score_final=body.score_final, author=who.login, role=who.role.value,
        comment=body.comment,
    )
    state = hub.get(session_id)
    if state is not None:
        _require_access(who, state)
        if state.score is None:
            raise HTTPException(status_code=409, detail="score_not_ready")
        scenario = scoring_scenario(state)
        if scenario is None:
            raise HTTPException(status_code=409, detail="scenario_not_found")
        async with hub.operation(session_id):
            override_score(state, correction)
        return build_report(session_id, state, scenario)

    # Занятия нет в памяти узла: правка из истории, по архивному разбору.
    if db is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    session = await repo.get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session_not_found")
    _require_access(who, session)
    score = await db.scalar(select(Score).where(Score.session_id == session_id))
    if score is None:
        raise HTTPException(status_code=409, detail="score_not_ready")
    archived = (score.report or {}).get("full_report")
    if archived is None:
        raise HTTPException(status_code=409, detail="report_not_archived")
    report = apply_score_override(dict(score.report), correction)
    await db.rollback()  # чтение закончено; запись — одной транзакцией хранилища
    await hub.store.commit_archived(session_id, [correction])
    return SessionReport.model_validate(report["full_report"])


@router.get("", response_model=list[SessionOut])
async def listing(
    request: Request,
    trainee: UUID | None = None,
    group: UUID | None = None,
    mode: SessionMode | None = None,
    since: datetime | None = Query(default=None, alias="from"),
    limit: int = 100,
    db: AsyncSession | None = Depends(optional_session),
) -> list[SessionOut]:
    who = require(request)
    # Обучающийся видит только свою историю, что бы он ни передал в фильтре.
    if who.role is Role.TRAINEE:
        if who.trainee_id is None:
            raise HTTPException(status_code=403, detail="trainee_profile_required")
        trainee = who.trainee_id
    owner_login = who.login if who.role is Role.INSTRUCTOR else None
    if db is None:
        # The explicit in-memory demo keeps completed session state in `hub`
        # until restart. It has no group records, so group-filtered history is
        # empty rather than silently leaking sessions outside that filter.
        if group is not None:
            return []
        states = hub.history(
            owner_login=owner_login,
            trainee_id=trainee,
            mode=mode.value if mode else None,
            since=since,
            limit=limit,
        )
        return [SessionOut(
            session_id=state.session_id,
            scenario_id=state.scenario_id,
            mode=state.mode,
            attempt=state.attempt,
            trainee_id=state.trainee_id,
            group_id=None,
            started_at=state.started_at,
            ended_at=state.ended_at,
            end_reason=state.end_reason.value if state.end_reason else None,
        ) for state in states]
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
