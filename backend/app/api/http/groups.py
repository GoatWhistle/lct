"""Сводка ошибок и рекомендаций учебной группы для преподавателя."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import add_audit_entry, audit_required, require
from app.db.base import get_session
from app.db.models import Group, Score, Session, Trainee, User
from app.domain.roles import Role
from app.scoring.group import ScoredAttempt, summarize
from app.scoring.group_insight import InsightInvalid, generate_group_insight
from app.dialog.llm import LlmUnavailable

router = APIRouter(prefix="/api/groups", tags=["groups"])


class GroupOut(BaseModel):
    id: UUID
    name: str


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class GroupOwnerPatch(BaseModel):
    owner_login: str | None


class GroupErrorOut(BaseModel):
    code: str
    title: str
    affected_trainees: int
    occurrences: int
    rate_percent: float
    recommendation: str


class GroupAnalyticsOut(BaseModel):
    group: GroupOut
    enrolled_trainees: int
    active_trainees: int
    scored_attempts: int
    average_score: float | None
    errors: list[GroupErrorOut]


class GroupInsightOut(BaseModel):
    summary: str
    priorities: list[str]
    source: str = "local_qwen"
    personal_data_sent: bool = False


async def _analytics(
    group_id: UUID, db: AsyncSession, *, owner_login: str | None = None
) -> GroupAnalyticsOut:
    group = await db.get(Group, group_id)
    if group is None or (owner_login is not None and group.owner_login != owner_login):
        raise HTTPException(status_code=404, detail="group_not_found")
    enrolled = await db.scalar(
        select(func.count()).select_from(Trainee).where(Trainee.group_id == group_id)
    )
    attempts_query = (
        select(Session.trainee_id, Score.score_final, Score.report)
        .join(Score, Score.session_id == Session.id)
        .join(Trainee, Trainee.id == Session.trainee_id, isouter=True)
        .where(
            or_(
                Session.group_id == group_id,
                and_(Session.group_id.is_(None), Trainee.group_id == group_id),
            ),
            Session.ended_at.is_not(None),
        )
    )
    if owner_login is not None:
        attempts_query = attempts_query.where(Session.owner_login == owner_login)
    rows = await db.execute(attempts_query)
    attempts = [
        ScoredAttempt(
            trainee_id=trainee_id,
            score=score,
            codes=(report or {}).get("summary", {}).get("codes", {}),
        )
        for trainee_id, score, report in rows
    ]
    return GroupAnalyticsOut(
        group=GroupOut(id=group.id, name=group.name),
        **summarize(attempts, int(enrolled or 0)),
    )


@router.get("", response_model=list[GroupOut])
async def listing(request: Request, db: AsyncSession = Depends(get_session)) -> list[GroupOut]:
    who = require(request, Role.INSTRUCTOR, Role.ADMIN)
    query = select(Group).order_by(Group.name)
    if who.role is Role.INSTRUCTOR:
        query = query.where(Group.owner_login == who.login)
    groups = await db.scalars(query)
    return [GroupOut(id=group.id, name=group.name) for group in groups]


@router.post("", response_model=GroupOut, status_code=201)
async def create(
    body: GroupCreate, request: Request, db: AsyncSession = Depends(get_session)
) -> GroupOut:
    who = require(request, Role.INSTRUCTOR, Role.ADMIN)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="group_name_required")
    group = Group(
        id=uuid4(), name=name, owner_login=who.login if who.role is Role.INSTRUCTOR else None
    )
    db.add(group)
    add_audit_entry(db, who.login, who.role.value, "group.create", str(group.id), group.name)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="group_exists") from exc
    return GroupOut(id=group.id, name=group.name)


@router.patch("/{group_id}/owner", response_model=GroupOut)
async def transfer_ownership(
    group_id: UUID,
    body: GroupOwnerPatch,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> GroupOut:
    """Администратор безопасно закрепляет legacy-группу за преподавателем."""
    who = require(request, Role.ADMIN)
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group_not_found")
    if body.owner_login is not None:
        user = await db.scalar(select(User).where(User.login == body.owner_login))
        if user is None or user.role != Role.INSTRUCTOR.value or user.blocked:
            raise HTTPException(status_code=422, detail="active_instructor_required")
    previous_owner = group.owner_login
    group.owner_login = body.owner_login
    add_audit_entry(
        db, who.login, who.role.value, "group.transfer", str(group.id),
        f"{previous_owner or 'admin'} -> {body.owner_login or 'admin'}",
    )
    await db.commit()
    return GroupOut(id=group.id, name=group.name)


@router.put("/{group_id}/trainees/{trainee_id}", response_model=GroupOut)
async def assign_trainee(
    group_id: UUID, trainee_id: UUID, request: Request,
    db: AsyncSession = Depends(get_session),
) -> GroupOut:
    who = require(request, Role.INSTRUCTOR, Role.ADMIN)
    group = await db.get(Group, group_id)
    if group is None or (who.role is Role.INSTRUCTOR and group.owner_login != who.login):
        raise HTTPException(status_code=404, detail="group_not_found")
    trainee = await db.get(Trainee, trainee_id)
    if trainee is None:
        raise HTTPException(status_code=404, detail="trainee_not_found")
    if (
        who.role is Role.INSTRUCTOR
        and trainee.group_id is not None
        and trainee.group_id != group_id
    ):
        current_group = await db.get(Group, trainee.group_id)
        if current_group is None or current_group.owner_login != who.login:
            raise HTTPException(status_code=409, detail="trainee_in_other_instructor_group")
    trainee.group_id = group_id
    add_audit_entry(db, who.login, who.role.value, "group.assign", str(group.id), str(trainee_id))
    await db.commit()
    return GroupOut(id=group.id, name=group.name)


@router.get("/{group_id}/analytics", response_model=GroupAnalyticsOut)
async def analytics(
    group_id: UUID, request: Request, db: AsyncSession = Depends(get_session)
) -> GroupAnalyticsOut:
    who = require(request, Role.INSTRUCTOR, Role.ADMIN)
    return await _analytics(
        group_id, db, owner_login=who.login if who.role is Role.INSTRUCTOR else None
    )


@router.post("/{group_id}/analytics/insight", response_model=GroupInsightOut)
async def ai_insight(
    group_id: UUID, request: Request, db: AsyncSession = Depends(get_session)
) -> GroupInsightOut:
    """Сформировать по запросу преподавателя локальный обезличенный инсайт."""
    who = require(request, Role.INSTRUCTOR)
    data = await _analytics(group_id, db, owner_login=who.login)
    if data.scored_attempts == 0:
        raise HTTPException(status_code=409, detail="no_scored_attempts")
    try:
        insight = await generate_group_insight(data.model_dump(mode="json"))
    except LlmUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"локальная модель недоступна: {exc}") from exc
    except InsightInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit_required(
        who.login, who.role.value, "group.ai_insight", str(group_id), "aggregated_only"
    )
    return GroupInsightOut(**insight)
