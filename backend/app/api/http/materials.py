"""Учебные материалы, локальные вложения и назначения курсантам.

Файлы хранятся в PostgreSQL и отдаются только как attachment: учебный контур
не зависит от внешнего файлового сервиса и не исполняет загруженный HTML.
"""

import base64
import binascii
import hashlib
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import DEMO_TRAINEE_ID, audit, require
from app.config import get_settings
from app.db.base import get_session
from app.db.models import Group, LearningMaterial, MaterialAssignment, Trainee
from app.domain.events import Exercise, ScenarioStart, SessionMode
from app.domain.roles import Role
from app.scenarios import store
from app.session.hub import hub

router = APIRouter(prefix="/api/materials", tags=["materials"])
MAX_FILE_BYTES = 5 * 1024 * 1024
LEVELS = {"L1", "L2", "L3"}

_DEMO_GUIDE_ID = UUID("00000000-0000-4000-8000-000000000901")
_demo_materials: dict[UUID, LearningMaterial] = {}
_demo_assignments: dict[tuple[UUID, UUID], dict] = {}


def reset_demo_materials() -> None:
    """Демонстрационный справочник воспроизводим после каждого старта."""
    _demo_materials.clear()
    _demo_assignments.clear()
    now = datetime.now(timezone.utc)
    _demo_materials[_DEMO_GUIDE_ID] = LearningMaterial(
        id=_DEMO_GUIDE_ID,
        title="Памятка диспетчера ДДС",
        description="Короткий алгоритм работы с готовой карточкой происшествия.",
        level="L1",
        kind="text",
        body=(
            "1. Подтвердите получение карточки.\n"
            "2. Проверьте зону ответственности и список оповещения.\n"
            "3. Назначьте бригаду и передайте адрес, событие и задачу.\n"
            "4. Фиксируйте выезд, прибытие, локализацию и завершение работ."
        ),
        scenario_id="t01-1-fire-container",
        file_name=None,
        media_type=None,
        file_data=None,
        file_sha256=None,
        active=True,
        created_by="system",
        created_at=now,
        updated_at=now,
    )
    _demo_assignments[(_DEMO_GUIDE_ID, DEMO_TRAINEE_ID)] = {
        "assigned_by": "system", "assigned_at": now, "completed_at": None,
    }


async def material_session() -> AsyncIterator[AsyncSession | None]:
    if get_settings().demo_no_db:
        yield None
    else:
        async for db in get_session():
            yield db


class MaterialCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=2000)
    level: Literal["L1", "L2", "L3"] = "L1"
    kind: Literal["text", "file"] = "text"
    body: str = Field(default="", max_length=30_000)
    scenario_id: str | None = Field(default=None, max_length=80)
    file_name: str | None = Field(default=None, max_length=240)
    media_type: str | None = Field(default=None, max_length=120)
    content_base64: str | None = None

    @model_validator(mode="after")
    def valid_content(self):
        if self.kind == "text" and not self.body.strip():
            raise ValueError("текст материала пуст")
        if self.kind == "file" and (not self.file_name or not self.content_base64):
            raise ValueError("для файла нужны имя и содержимое")
        return self


class MaterialPatch(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    level: Literal["L1", "L2", "L3"] | None = None
    body: str | None = Field(default=None, max_length=30_000)
    scenario_id: str | None = Field(default=None, max_length=80)
    active: bool | None = None


class MaterialOut(BaseModel):
    id: UUID
    title: str
    description: str
    level: str
    kind: str
    body: str
    scenario_id: str | None
    file_name: str | None
    media_type: str | None
    file_size: int
    file_sha256: str | None
    active: bool
    created_by: str
    created_at: datetime
    assigned_at: datetime | None = None
    completed_at: datetime | None = None
    assignment_count: int = 0


def _out(
    row: LearningMaterial, *, assignment: MaterialAssignment | dict | None = None,
    assignment_count: int = 0,
) -> MaterialOut:
    if isinstance(assignment, dict):
        assigned_at = assignment.get("assigned_at")
        completed_at = assignment.get("completed_at")
    else:
        assigned_at = assignment.assigned_at if assignment else None
        completed_at = assignment.completed_at if assignment else None
    return MaterialOut(
        id=row.id,
        title=row.title,
        description=row.description,
        level=row.level,
        kind=row.kind,
        body=row.body,
        scenario_id=row.scenario_id,
        file_name=row.file_name,
        media_type=row.media_type,
        file_size=len(row.file_data or b""),
        file_sha256=row.file_sha256,
        active=row.active,
        created_by=row.created_by,
        created_at=row.created_at,
        assigned_at=assigned_at,
        completed_at=completed_at,
        assignment_count=assignment_count,
    )


def _decode_file(payload: MaterialCreate) -> bytes | None:
    if payload.kind != "file":
        return None
    try:
        data = base64.b64decode(payload.content_base64 or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid_file_base64") from exc
    if not data:
        raise HTTPException(status_code=422, detail="empty_file")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="file_too_large_5mb")
    return data


def _validate_scenario(scenario_id: str | None) -> None:
    if scenario_id and store.get(scenario_id) is None:
        raise HTTPException(status_code=422, detail="scenario_not_found")


def _safe_filename(value: str) -> str:
    """Убрать и POSIX-, и Windows-путь; в БД остаётся только имя файла."""
    return value.replace("\\", "/").rsplit("/", 1)[-1] or "resource.bin"


@router.get("", response_model=list[MaterialOut])
async def listing(
    request: Request,
    include_archived: bool = False,
    db: AsyncSession | None = Depends(material_session),
) -> list[MaterialOut]:
    who = require(request)
    if db is None:
        if who.role is Role.TRAINEE:
            if who.trainee_id is None:
                raise HTTPException(status_code=403, detail="trainee_profile_required")
            result = []
            for material_id, row in _demo_materials.items():
                assignment = _demo_assignments.get((material_id, who.trainee_id))
                if assignment and row.active:
                    result.append(_out(row, assignment=assignment))
            return result
        return [
            _out(row, assignment_count=sum(mid == row.id for mid, _ in _demo_assignments))
            for row in _demo_materials.values()
            if (include_archived or row.active)
            and (who.role is not Role.INSTRUCTOR or row.created_by in {who.login, "system"})
        ]

    if who.role is Role.TRAINEE:
        if who.trainee_id is None:
            raise HTTPException(status_code=403, detail="trainee_profile_required")
        rows = await db.execute(
            select(LearningMaterial, MaterialAssignment)
            .join(MaterialAssignment, MaterialAssignment.material_id == LearningMaterial.id)
            .where(
                MaterialAssignment.trainee_id == who.trainee_id,
                LearningMaterial.active.is_(True),
            )
            .order_by(MaterialAssignment.assigned_at.desc())
        )
        return [_out(row, assignment=assignment) for row, assignment in rows]

    require(request, Role.INSTRUCTOR, Role.ADMIN)
    statement = (
        select(LearningMaterial, func.count(MaterialAssignment.id))
        .outerjoin(MaterialAssignment, MaterialAssignment.material_id == LearningMaterial.id)
        .group_by(LearningMaterial.id)
        .order_by(LearningMaterial.active.desc(), LearningMaterial.updated_at.desc())
    )
    if who.role is Role.INSTRUCTOR:
        statement = statement.where(LearningMaterial.created_by == who.login)
    if not include_archived:
        statement = statement.where(LearningMaterial.active.is_(True))
    rows = await db.execute(statement)
    return [_out(row, assignment_count=count) for row, count in rows]


@router.post("", response_model=MaterialOut, status_code=201)
async def create(
    payload: MaterialCreate,
    request: Request,
    db: AsyncSession | None = Depends(material_session),
) -> MaterialOut:
    who = require(request, Role.INSTRUCTOR)
    _validate_scenario(payload.scenario_id)
    data = _decode_file(payload)
    now = datetime.now(timezone.utc)
    row = LearningMaterial(
        id=uuid4(),
        title=payload.title.strip(),
        description=payload.description.strip(),
        level=payload.level,
        kind=payload.kind,
        body=payload.body.strip(),
        scenario_id=payload.scenario_id,
        file_name=_safe_filename(payload.file_name) if payload.file_name else None,
        media_type=(payload.media_type or "application/octet-stream") if data else None,
        file_data=data,
        file_sha256=hashlib.sha256(data).hexdigest() if data else None,
        active=True,
        created_by=who.login,
        created_at=now,
        updated_at=now,
    )
    if db is None:
        _demo_materials[row.id] = row
    else:
        db.add(row)
        await db.commit()
    await audit(who.login, who.role.value, "material.create", str(row.id), row.title)
    return _out(row)


async def _material(db: AsyncSession | None, material_id: UUID) -> LearningMaterial | None:
    return _demo_materials.get(material_id) if db is None else await db.get(LearningMaterial, material_id)


def _require_owner(row: LearningMaterial, login: str) -> None:
    """Only the instructor who authored a resource may manage it."""
    if row.created_by != login:
        raise HTTPException(status_code=404, detail="material_not_found")


@router.patch("/{material_id}", response_model=MaterialOut)
async def update(
    material_id: UUID,
    payload: MaterialPatch,
    request: Request,
    db: AsyncSession | None = Depends(material_session),
) -> MaterialOut:
    who = require(request, Role.INSTRUCTOR)
    row = await _material(db, material_id)
    if row is None:
        raise HTTPException(status_code=404, detail="material_not_found")
    _require_owner(row, who.login)
    patch = payload.model_dump(exclude_unset=True)
    if "scenario_id" in patch:
        patch["scenario_id"] = patch["scenario_id"] or None
        _validate_scenario(patch["scenario_id"])
    if row.kind == "text" and "body" in patch and not (patch["body"] or "").strip():
        raise HTTPException(status_code=422, detail="empty_material_body")
    for key, value in patch.items():
        setattr(row, key, value.strip() if isinstance(value, str) else value)
    row.updated_at = datetime.now(timezone.utc)
    if db is not None:
        await db.commit()
    await audit(who.login, who.role.value, "material.update", str(row.id))
    return _out(row)


@router.delete("/{material_id}", response_model=MaterialOut)
async def archive(
    material_id: UUID,
    request: Request,
    db: AsyncSession | None = Depends(material_session),
) -> MaterialOut:
    who = require(request, Role.INSTRUCTOR)
    row = await _material(db, material_id)
    if row is None:
        raise HTTPException(status_code=404, detail="material_not_found")
    _require_owner(row, who.login)
    row.active = False
    row.updated_at = datetime.now(timezone.utc)
    if db is not None:
        await db.commit()
    await audit(who.login, who.role.value, "material.archive", str(row.id))
    return _out(row)


@router.put("/{material_id}/assign/{trainee_id}", response_model=MaterialOut)
async def assign(
    material_id: UUID,
    trainee_id: UUID,
    request: Request,
    db: AsyncSession | None = Depends(material_session),
) -> MaterialOut:
    who = require(request, Role.INSTRUCTOR)
    row = await _material(db, material_id)
    if row is None or not row.active:
        raise HTTPException(status_code=404, detail="material_not_found")
    _require_owner(row, who.login)
    if db is None:
        if trainee_id != DEMO_TRAINEE_ID:
            raise HTTPException(status_code=404, detail="trainee_not_found")
        assignment = _demo_assignments.setdefault(
            (material_id, trainee_id),
            {"assigned_by": who.login, "assigned_at": datetime.now(timezone.utc), "completed_at": None},
        )
    else:
        trainee = await db.get(Trainee, trainee_id)
        if trainee is None:
            raise HTTPException(status_code=404, detail="trainee_not_found")
        if trainee.group_id is not None:
            group = await db.get(Group, trainee.group_id)
            if group is None or group.owner_login != who.login:
                raise HTTPException(status_code=404, detail="trainee_not_found")
        assignment = await db.scalar(select(MaterialAssignment).where(
            MaterialAssignment.material_id == material_id,
            MaterialAssignment.trainee_id == trainee_id,
        ))
        if assignment is None:
            assignment = MaterialAssignment(
                material_id=material_id, trainee_id=trainee_id, assigned_by=who.login
            )
            db.add(assignment)
            await db.commit()
            await db.refresh(assignment)
    await audit(who.login, who.role.value, "material.assign", str(row.id), str(trainee_id))
    return _out(row, assignment=assignment)


@router.put("/{material_id}/assign-group/{group_id}")
async def assign_group(
    material_id: UUID,
    group_id: UUID,
    request: Request,
    db: AsyncSession | None = Depends(material_session),
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    row = await _material(db, material_id)
    if row is None or not row.active:
        raise HTTPException(status_code=404, detail="material_not_found")
    _require_owner(row, who.login)
    if db is None:
        raise HTTPException(status_code=409, detail="groups_unavailable_in_demo")
    group = await db.get(Group, group_id)
    if group is None or group.owner_login != who.login:
        raise HTTPException(status_code=404, detail="group_not_found")
    trainee_ids = list(await db.scalars(select(Trainee.id).where(Trainee.group_id == group_id)))
    existing = set(await db.scalars(select(MaterialAssignment.trainee_id).where(
        MaterialAssignment.material_id == material_id,
        MaterialAssignment.trainee_id.in_(trainee_ids),
    ))) if trainee_ids else set()
    for trainee_id in trainee_ids:
        if trainee_id not in existing:
            db.add(MaterialAssignment(
                material_id=material_id, trainee_id=trainee_id, assigned_by=who.login
            ))
    await db.commit()
    await audit(who.login, who.role.value, "material.assign_group", str(row.id), str(group_id))
    return {"material_id": str(row.id), "assigned": len(trainee_ids)}


@router.delete("/{material_id}/assign/{trainee_id}")
async def unassign(
    material_id: UUID,
    trainee_id: UUID,
    request: Request,
    db: AsyncSession | None = Depends(material_session),
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    row = await _material(db, material_id)
    if row is None:
        raise HTTPException(status_code=404, detail="material_not_found")
    _require_owner(row, who.login)
    if db is None:
        removed = _demo_assignments.pop((material_id, trainee_id), None) is not None
    else:
        assignment = await db.scalar(select(MaterialAssignment).where(
            MaterialAssignment.material_id == material_id,
            MaterialAssignment.trainee_id == trainee_id,
        ))
        if assignment is not None and assignment.assigned_by != who.login:
            raise HTTPException(status_code=404, detail="assignment_not_found")
        if assignment is not None:
            await db.delete(assignment)
            await db.commit()
        removed = assignment is not None
    await audit(who.login, who.role.value, "material.unassign", str(material_id), str(trainee_id))
    return {"removed": removed}


@router.post("/{material_id}/complete", response_model=MaterialOut)
async def complete(
    material_id: UUID,
    request: Request,
    db: AsyncSession | None = Depends(material_session),
) -> MaterialOut:
    who = require(request, Role.TRAINEE)
    if who.trainee_id is None:
        raise HTTPException(status_code=403, detail="trainee_profile_required")
    row = await _material(db, material_id)
    if row is None or not row.active:
        raise HTTPException(status_code=404, detail="material_not_found")
    if db is None:
        assignment = _demo_assignments.get((material_id, who.trainee_id))
    else:
        assignment = await db.scalar(select(MaterialAssignment).where(
            MaterialAssignment.material_id == material_id,
            MaterialAssignment.trainee_id == who.trainee_id,
        ))
    if assignment is None:
        raise HTTPException(status_code=403, detail="material_not_assigned")
    completed_at = datetime.now(timezone.utc)
    if isinstance(assignment, dict):
        assignment["completed_at"] = completed_at
    else:
        assignment.completed_at = completed_at
        await db.commit()
    await audit(who.login, who.role.value, "material.complete", str(material_id))
    return _out(row, assignment=assignment)


@router.post("/{material_id}/start")
async def start_assigned_practice(
    material_id: UUID,
    request: Request,
    db: AsyncSession | None = Depends(material_session),
) -> dict:
    """Курсант сам выбирает назначенный модуль и запускает текстовую практику."""
    who = require(request, Role.TRAINEE)
    if who.trainee_id is None:
        raise HTTPException(status_code=403, detail="trainee_profile_required")
    row = await _material(db, material_id)
    if row is None or not row.active:
        raise HTTPException(status_code=404, detail="material_not_found")
    if db is None:
        assignment = _demo_assignments.get((material_id, who.trainee_id))
    else:
        assignment = await db.scalar(select(MaterialAssignment).where(
            MaterialAssignment.material_id == material_id,
            MaterialAssignment.trainee_id == who.trainee_id,
        ))
    if assignment is None:
        raise HTTPException(status_code=403, detail="material_not_assigned")
    if not row.scenario_id:
        raise HTTPException(status_code=409, detail="material_has_no_practice")
    scenario = store.get(row.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=409, detail="scenario_not_found")
    # Явное назначение преподавателя — и есть разрешение на самостоятельный
    # модуль. Оно не открывает курсанту остальные сценарии библиотеки.

    from app.api.ws.control import _start

    session_id = uuid4()
    await _start(session_id, ScenarioStart(
        scenario_id=scenario.id,
        trainee=who.full_name,
        trainee_id=who.trainee_id,
        mode=SessionMode.SELF,
        exercise=Exercise.CARD,
    ), who)
    if hub.get(session_id) is None:
        raise HTTPException(status_code=409, detail="practice_start_failed")
    return {
        "session_id": str(session_id),
        "scenario_id": scenario.id,
        "mode": SessionMode.SELF.value,
        "exercise": Exercise.CARD.value,
        "path": f"/trainee?session={session_id}",
    }


@router.get("/{material_id}/download")
async def download(
    material_id: UUID,
    request: Request,
    db: AsyncSession | None = Depends(material_session),
) -> Response:
    who = require(request)
    row = await _material(db, material_id)
    if row is None or not row.active or row.kind != "file" or row.file_data is None:
        raise HTTPException(status_code=404, detail="file_not_found")
    if who.role is Role.TRAINEE:
        if who.trainee_id is None:
            raise HTTPException(status_code=403, detail="trainee_profile_required")
        if db is None:
            allowed = (material_id, who.trainee_id) in _demo_assignments
        else:
            allowed = await db.scalar(select(MaterialAssignment.id).where(
                MaterialAssignment.material_id == material_id,
                MaterialAssignment.trainee_id == who.trainee_id,
            )) is not None
        if not allowed:
            raise HTTPException(status_code=403, detail="material_not_assigned")
    elif who.role is Role.INSTRUCTOR:
        _require_owner(row, who.login)
    elif who.role not in {Role.INSTRUCTOR, Role.ADMIN}:
        raise HTTPException(status_code=403, detail="forbidden")
    filename = _safe_filename(row.file_name or "resource.bin")
    disposition = f"attachment; filename=resource; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=row.file_data,
        media_type=row.media_type or "application/octet-stream",
        headers={
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
        },
    )
