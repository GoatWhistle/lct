"""Профиль курсанта и прогресс.

«Контроль подготовленности» из ТЗ — не таблица оценок, а инструмент планирования
подготовки: радар компетенций, история попыток и дельта между ними
(docs/product/MODES.md#профиль-курсанта). Считается по журналу занятий.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import DEMO_TRAINEE_ID, audit_required, require
from app.config import get_settings
from app.domain.roles import Role
from app.db.base import get_session, get_sessionmaker
from app.db.models import Group, Score, Session, Trainee, User
from app.domain.taxonomy import ERRORS, ErrorCode
from app.scoring.export import certificate_pdf
from app.scoring.group import RECOMMENDATIONS
from app.voice.recording import recording_path

router = APIRouter(prefix="/api/trainees", tags=["trainees"])


@router.get("/{trainee_id}/certificate.pdf")
async def certificate(
    trainee_id: UUID, request: Request, db: AsyncSession = Depends(get_session)
) -> Response:
    who = require(request)
    if who.role is Role.TRAINEE and who.trainee_id != trainee_id:
        raise HTTPException(status_code=403, detail="not_your_certificate")
    if who.role is Role.INSTRUCTOR:
        owns_trainee = await db.scalar(
            select(Session.id)
            .where(Session.trainee_id == trainee_id, Session.owner_login == who.login)
            .limit(1)
        )
        if owns_trainee is None:
            raise HTTPException(status_code=404, detail="trainee_not_found")
    trainee = await db.get(Trainee, trainee_id)
    if trainee is None:
        raise HTTPException(status_code=404, detail="trainee_not_found")
    group = await db.get(Group, trainee.group_id) if trainee.group_id else None
    if who.role is Role.INSTRUCTOR and group is not None and group.owner_login != who.login:
        group = None
    scores_query = (
        select(func.count(Score.id), func.avg(Score.score_final), func.max(Score.created_at))
        .join(Session, Session.id == Score.session_id)
        .where(Session.trainee_id == trainee_id)
    )
    if who.role is Role.INSTRUCTOR:
        scores_query = scores_query.where(Session.owner_login == who.login)
    result = await db.execute(scores_query)
    attempts, average_score, completed_at = result.one()
    if not attempts:
        raise HTTPException(status_code=409, detail="no_scored_attempts")
    try:
        content = certificate_pdf(
            trainee_name=trainee.name,
            trainee_id=trainee.id,
            group_name=group.name if group else None,
            attempts=int(attempts),
            average_score=float(average_score),
            issued_at=completed_at.date().isoformat(),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await audit_required(
        who.login, who.role.value, "trainee.certificate.export.pdf", str(trainee_id)
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="trainee-{trainee_id}-certificate.pdf"'},
    )


class TraineeOut(BaseModel):
    id: UUID
    name: str
    group: str | None = None
    service: str | None = None


class AttemptOut(BaseModel):
    session_id: UUID
    scenario_id: str
    mode: str
    attempt: int
    created_at: datetime
    score: float | None = None
    interview_ms: int | None = None
    facts_got: int | None = None
    facts_required: int | None = None
    hints: int | None = None
    codes: dict[str, int] = {}
    recording_available: bool = False


class DeltaOut(BaseModel):
    """Что изменилось от попытки к попытке — измеримый цикл работы над ошибками."""

    scenario_id: str
    from_attempt: int
    to_attempt: int
    score: float | None = None
    interview_ms: int | None = None
    facts_got: int | None = None


class RecommendationOut(BaseModel):
    code: str
    title: str
    recommendation: str
    occurrences: int


class ProfileOut(BaseModel):
    trainee: TraineeOut
    attempts: list[AttemptOut]
    competencies: dict[str, float]
    deltas: list[DeltaOut]
    recommendations: list[RecommendationOut]
    hints_total: int


def _personal_recommendations(codes: dict[str, int]) -> list[RecommendationOut]:
    """Следующие упражнения опираются на коды последней оценённой попытки."""
    recommendations = []
    for code, count in codes.items():
        if code not in RECOMMENDATIONS or not isinstance(count, int) or count <= 0:
            continue
        error = ERRORS[ErrorCode(code)]
        recommendations.append(RecommendationOut(
            code=code,
            title=error.title,
            recommendation=RECOMMENDATIONS[code],
            occurrences=count,
        ))
    return sorted(recommendations, key=lambda item: (-item.occurrences, item.code))[:5]


@router.get("", response_model=list[TraineeOut])
async def listing(request: Request) -> list[TraineeOut]:
    """Список курсантов — преподавателю и администратору: обучающемуся он
    не нужен, а чужие фамилии из него видны."""
    who = require(request, Role.INSTRUCTOR, Role.ADMIN)
    if get_settings().demo_no_db:
        return [TraineeOut(id=DEMO_TRAINEE_ID, name="Демо-курсант", service="Служба 101")]
    async with get_sessionmaker()() as db:
        query = (
            select(Trainee, Group.name, Group.owner_login, User.service)
            .join(Group, Group.id == Trainee.group_id, isouter=True)
            .join(User, User.trainee_id == Trainee.id, isouter=True)
        )
        if who.role is Role.INSTRUCTOR:
            has_owned_attempt = exists(
                select(Session.id).where(
                    Session.trainee_id == Trainee.id,
                    Session.owner_login == who.login,
                )
            )
            query = query.where(
                or_(
                    Trainee.group_id.is_(None),
                    Group.owner_login == who.login,
                    has_owned_attempt,
                )
            )
        rows = await db.execute(query)
        return [
            TraineeOut(
                id=trainee.id,
                name=trainee.name,
                group=(group if who.role is not Role.INSTRUCTOR or group_owner == who.login else None),
                service=service,
            )
            for trainee, group, group_owner, service in rows
        ]


@router.get("/{trainee_id}/profile", response_model=ProfileOut)
async def profile(
    trainee_id: UUID, request: Request, db: AsyncSession = Depends(get_session)
) -> ProfileOut:
    who = require(request)
    if who.role is Role.TRAINEE and who.trainee_id != trainee_id:
        raise HTTPException(status_code=403, detail="not_your_profile")
    if who.role is Role.INSTRUCTOR:
        owns_trainee = await db.scalar(
            select(Session.id)
            .where(Session.trainee_id == trainee_id, Session.owner_login == who.login)
            .limit(1)
        )
        if owns_trainee is None:
            raise HTTPException(status_code=404, detail="trainee_not_found")
    trainee = await db.get(Trainee, trainee_id)
    if trainee is None:
        raise HTTPException(status_code=404, detail="trainee_not_found")
    group = await db.get(Group, trainee.group_id) if trainee.group_id else None
    if who.role is Role.INSTRUCTOR and group is not None and group.owner_login != who.login:
        group = None

    attempts_query = (
        select(Session, Score)
        .join(Score, Score.session_id == Session.id, isouter=True)
        .where(Session.trainee_id == trainee_id)
        .order_by(Session.created_at)
    )
    if who.role is Role.INSTRUCTOR:
        attempts_query = attempts_query.where(Session.owner_login == who.login)
    rows = await db.execute(attempts_query)
    attempts: list[AttemptOut] = []
    competency_sums: dict[str, list[float]] = {}
    latest_scored_codes: dict[str, int] = {}
    for session, score in rows:
        summary = (score.report or {}).get("summary", {}) if score else {}
        if score is not None:
            latest_scored_codes = summary.get("codes", {})
        attempts.append(
            AttemptOut(
                session_id=session.id,
                scenario_id=session.scenario_id,
                mode=session.mode,
                attempt=session.attempt,
                created_at=session.created_at,
                score=score.score_final if score else None,
                interview_ms=summary.get("interview_ms"),
                facts_got=summary.get("facts_got"),
                facts_required=summary.get("facts_required"),
                hints=summary.get("hints"),
                codes=summary.get("codes", {}),
                recording_available=recording_path(session.id).is_file(),
            )
        )
        for item in (score.report or {}).get("competencies", []) if score else []:
            competency_sums.setdefault(item["competency"], []).append(item["value"])

    # Радар — среднее по попыткам: одна неудачная попытка не определяет курсанта.
    competencies = {
        name: round(sum(values) / len(values), 3) for name, values in competency_sums.items()
    }

    deltas: list[DeltaOut] = []
    by_scenario: dict[str, list[AttemptOut]] = {}
    for attempt in attempts:
        by_scenario.setdefault(attempt.scenario_id, []).append(attempt)
    for scenario_id, items in by_scenario.items():
        for previous, current in zip(items, items[1:]):
            deltas.append(
                DeltaOut(
                    scenario_id=scenario_id,
                    from_attempt=previous.attempt,
                    to_attempt=current.attempt,
                    score=_diff(previous.score, current.score),
                    interview_ms=_diff(previous.interview_ms, current.interview_ms),
                    facts_got=_diff(previous.facts_got, current.facts_got),
                )
            )

    result = ProfileOut(
        trainee=TraineeOut(id=trainee.id, name=trainee.name, group=group.name if group else None),
        attempts=attempts,
        competencies=competencies,
        deltas=deltas,
        recommendations=_personal_recommendations(latest_scored_codes),
        hints_total=sum(attempt.hints or 0 for attempt in attempts),
    )
    await audit_required(
        who.login, who.role.value, "trainee.profile.read", str(trainee_id)
    )
    return result


def _diff(before, after):
    if before is None or after is None:
        return None
    return round(after - before, 3)
