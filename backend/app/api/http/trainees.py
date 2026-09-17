"""Профиль курсанта и прогресс.

«Контроль подготовленности» из ТЗ — не таблица оценок, а инструмент планирования
подготовки: радар компетенций, история попыток и дельта между ними
(docs/product/MODES.md#профиль-курсанта). Считается по журналу занятий.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import get_session
from app.db.models import Group, Score, Session, Trainee

router = APIRouter(prefix="/api/trainees", tags=["trainees"])


class TraineeOut(BaseModel):
    id: UUID
    name: str
    group: str | None = None


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


class DeltaOut(BaseModel):
    """Что изменилось от попытки к попытке — измеримый цикл работы над ошибками."""

    scenario_id: str
    from_attempt: int
    to_attempt: int
    score: float | None = None
    interview_ms: int | None = None
    facts_got: int | None = None


class ProfileOut(BaseModel):
    trainee: TraineeOut
    attempts: list[AttemptOut]
    competencies: dict[str, float]
    deltas: list[DeltaOut]
    hints_total: int


@router.get("", response_model=list[TraineeOut])
async def listing(db: AsyncSession = Depends(get_session)) -> list[TraineeOut]:
    rows = await db.execute(
        select(Trainee, Group.name).join(Group, Group.id == Trainee.group_id, isouter=True)
    )
    return [TraineeOut(id=trainee.id, name=trainee.name, group=group) for trainee, group in rows]


@router.get("/{trainee_id}/profile", response_model=ProfileOut)
async def profile(trainee_id: UUID, db: AsyncSession = Depends(get_session)) -> ProfileOut:
    trainee = await db.get(Trainee, trainee_id)
    if trainee is None:
        raise HTTPException(status_code=404, detail="trainee_not_found")
    group = await db.get(Group, trainee.group_id) if trainee.group_id else None

    rows = await db.execute(
        select(Session, Score)
        .join(Score, Score.session_id == Session.id, isouter=True)
        .where(Session.trainee_id == trainee_id)
        .order_by(Session.created_at)
    )
    attempts: list[AttemptOut] = []
    competency_sums: dict[str, list[float]] = {}
    for session, score in rows:
        summary = (score.report or {}).get("summary", {}) if score else {}
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

    return ProfileOut(
        trainee=TraineeOut(id=trainee.id, name=trainee.name, group=group.name if group else None),
        attempts=attempts,
        competencies=competencies,
        deltas=deltas,
        hints_total=sum(attempt.hints or 0 for attempt in attempts),
    )


def _diff(before, after):
    if before is None or after is None:
        return None
    return round(after - before, 3)
