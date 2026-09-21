"""Сводка ошибок и рекомендаций учебной группы для преподавателя."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import audit, require
from app.db.base import get_session
from app.db.models import Group, Score, Session, Trainee
from app.domain.roles import Role
from app.scoring.group import ScoredAttempt, summarize

router = APIRouter(prefix="/api/groups", tags=["groups"])


class GroupOut(BaseModel):
    id: UUID
    name: str


class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


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


@router.get("", response_model=list[GroupOut])
async def listing(request: Request, db: AsyncSession = Depends(get_session)) -> list[GroupOut]:
    require(request, Role.INSTRUCTOR, Role.ADMIN)
    groups = await db.scalars(select(Group).order_by(Group.name))
    return [GroupOut(id=group.id, name=group.name) for group in groups]


@router.post("", response_model=GroupOut, status_code=201)
async def create(
    body: GroupCreate, request: Request, db: AsyncSession = Depends(get_session)
) -> GroupOut:
    who = require(request, Role.INSTRUCTOR, Role.ADMIN)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="group_name_required")
    group = Group(name=name)
    db.add(group)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="group_exists") from exc
    await audit(who.login, who.role.value, "group.create", str(group.id), group.name)
    return GroupOut(id=group.id, name=group.name)


@router.put("/{group_id}/trainees/{trainee_id}", response_model=GroupOut)
async def assign_trainee(
    group_id: UUID, trainee_id: UUID, request: Request,
    db: AsyncSession = Depends(get_session),
) -> GroupOut:
    who = require(request, Role.INSTRUCTOR, Role.ADMIN)
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group_not_found")
    trainee = await db.get(Trainee, trainee_id)
    if trainee is None:
        raise HTTPException(status_code=404, detail="trainee_not_found")
    trainee.group_id = group_id
    await db.commit()
    await audit(who.login, who.role.value, "group.assign", str(group.id), str(trainee_id))
    return GroupOut(id=group.id, name=group.name)


@router.get("/{group_id}/analytics", response_model=GroupAnalyticsOut)
async def analytics(
    group_id: UUID, request: Request, db: AsyncSession = Depends(get_session)
) -> GroupAnalyticsOut:
    require(request, Role.INSTRUCTOR, Role.ADMIN)
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group_not_found")
    enrolled = await db.scalar(
        select(func.count()).select_from(Trainee).where(Trainee.group_id == group_id)
    )
    rows = await db.execute(
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
