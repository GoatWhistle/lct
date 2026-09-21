"""АРМ администратора: учётные записи, состояние стенда, аудит, копии.

ТЗ выделяет администратора отдельной ролью с собственным разделом: управление
учётными записями, состояние компонентов, резервное копирование не реже раза
в сутки, журналы и мониторинг нагрузки.

Границы роли из ТЗ соблюдаются здесь же: администратор **не** правит оценки
и не вмешивается в занятия — этих точек в модуле нет вовсе, а не «есть,
но с проверкой».
"""

import logging
import re
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.admin import backup as backup_service
from app.api.auth import audit, hash_password, invalidate_login, require
from app.config import get_settings
from app.db.base import get_session
from app.db.models import AuditLog, Session as SessionRow, Trainee, User
from app.domain import ekp
from app.domain.roles import ROLE_LABELS, Role
from app.session.hub import hub

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])


class UserOut(BaseModel):
    id: UUID
    login: str
    full_name: str
    role: Role
    service: str | None
    blocked: bool
    created_at: datetime


class UserCreate(BaseModel):
    login: str = Field(min_length=3, max_length=80)
    full_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, description="Короткий пароль не заводится")
    role: Role
    service: str | None = None


class UserPatch(BaseModel):
    """Что администратор меняет у существующей записи. Пароль — сбросом,
    а не чтением: старый он не видит и увидеть не может."""

    role: Role | None = None
    service: str | None = None
    blocked: bool | None = None
    password: str | None = Field(default=None, min_length=8)


def _out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        login=user.login,
        full_name=user.full_name,
        role=Role(user.role),
        service=user.service,
        blocked=user.blocked,
        created_at=user.created_at,
    )


@router.get("/users", response_model=list[UserOut])
async def users(request: Request, db: AsyncSession = Depends(get_session)) -> list[UserOut]:
    require(request, Role.ADMIN)
    rows = await db.scalars(select(User).order_by(User.created_at))
    return [_out(user) for user in rows]


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    body: UserCreate, request: Request, db: AsyncSession = Depends(get_session)
) -> UserOut:
    who = require(request, Role.ADMIN)

    user = User(
        login=body.login,
        full_name=body.full_name,
        password_hash=hash_password(body.password),
        role=body.role.value,
        service=body.service,
    )
    # У обучающегося должна быть карточка курсанта: на ней висят профиль,
    # история и проверка «это твой разбор» (lct-23).
    if body.role is Role.TRAINEE:
        trainee = Trainee(name=body.full_name)
        db.add(trainee)
        await db.flush()
        user.trainee_id = trainee.id

    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail="login_taken") from exc

    await audit(who.login, who.role.value, "user.create", body.login, ROLE_LABELS[body.role])
    return _out(user)


@router.patch("/users/{user_id}", response_model=UserOut)
async def patch_user(
    user_id: UUID, body: UserPatch, request: Request, db: AsyncSession = Depends(get_session)
) -> UserOut:
    who = require(request, Role.ADMIN)
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user_not_found")

    changed: list[str] = []
    if body.role is not None:
        if user.login == who.login and body.role is not Role.ADMIN:
            raise HTTPException(status_code=409, detail="cannot_demote_yourself")
        if body.role is Role.TRAINEE and user.trainee_id is None:
            trainee = Trainee(name=user.full_name)
            db.add(trainee)
            await db.flush()
            user.trainee_id = trainee.id
        user.role = body.role.value
        changed.append(f"роль {body.role.value}")
    if body.service is not None:
        user.service = body.service
        changed.append(f"служба {body.service}")
    if body.blocked is not None:
        # Блокировка себя оставила бы стенд без администратора до похода в базу.
        if body.blocked and user.login == who.login:
            raise HTTPException(status_code=409, detail="cannot_block_yourself")
        user.blocked = body.blocked
        changed.append("заблокирован" if body.blocked else "разблокирован")
    if body.password is not None:
        user.password_hash = hash_password(body.password)
        changed.append("пароль сброшен")

    if not changed:
        return _out(user)
    await db.commit()
    invalidate_login(user.login)
    await audit(who.login, who.role.value, "user.update", user.login, ", ".join(changed))
    return _out(user)


class AuditOut(BaseModel):
    at: datetime
    actor: str
    role: str
    action: str
    object_id: str | None
    detail: str


@router.get("/audit", response_model=list[AuditOut])
async def audit_log(
    request: Request,
    action: str | None = None,
    actor: str | None = None,
    limit: int = 200,
    db: AsyncSession = Depends(get_session),
) -> list[AuditOut]:
    """Журнал действий. Администратор его читает, но не правит: точки удаления
    или изменения записи здесь нет — ТЗ требует хранения, а не управления."""
    require(request, Role.ADMIN)
    query = select(AuditLog).order_by(AuditLog.at.desc()).limit(max(1, min(limit, 1000)))
    if action:
        query = query.where(AuditLog.action == action)
    if actor:
        query = query.where(AuditLog.actor == actor)
    rows = await db.scalars(query)
    return [
        AuditOut(
            at=row.at, actor=row.actor, role=row.role,
            action=row.action, object_id=row.object_id, detail=row.detail,
        )
        for row in rows
    ]


class ServiceState(BaseModel):
    name: str
    ok: bool
    detail: str


@router.get("/status", response_model=list[ServiceState])
async def status(request: Request, db: AsyncSession = Depends(get_session)) -> list[ServiceState]:
    """Состояние компонентов стенда — то, что администратор смотрит до занятия,
    а не после жалобы преподавателя."""
    require(request, Role.ADMIN)
    from app.main import app
    from app.voice.models import get_voice_models

    settings = get_settings()
    states: list[ServiceState] = []

    try:
        sessions_total = await db.scalar(select(func.count()).select_from(SessionRow))
        states.append(ServiceState(name="База данных", ok=True, detail=f"занятий в журнале: {sessions_total}"))
    except Exception as exc:  # noqa: BLE001
        states.append(ServiceState(name="База данных", ok=False, detail=str(exc)[:200]))

    models_ready = getattr(app.state, "models_ready", False) or get_voice_models() is not None
    states.append(
        ServiceState(
            name="Модели речи",
            ok=bool(models_ready),
            detail="распознавание и синтез готовы" if models_ready else "не загружены: занятие пойдёт без голоса",
        )
    )
    states.append(
        ServiceState(
            name="Эмбеддинги",
            ok=bool(getattr(app.state, "embeddings_ready", False)),
            detail="слот-автомат работает" if getattr(app.state, "embeddings_ready", False)
            else "нет модели: подсказки идут по порядку чек-листа",
        )
    )
    states.append(
        ServiceState(
            name="Провайдер LLM",
            ok=bool(settings.llm_api_key and settings.llm_base_url),
            detail=settings.llm_base_url or "не настроен: звонящий читает офлайн-таблицу",
        )
    )

    reference = ekp.reference()
    states.append(
        ServiceState(
            name="Классификатор ЕКП",
            ok=True,
            detail=f"версия {reference.version}, кодов {len(reference.incidents)}",
        )
    )
    states.append(
        ServiceState(
            name="Живых занятий",
            ok=True,
            detail=str(len(hub._sessions)),  # noqa: SLF001 — реестр в памяти процесса
        )
    )
    # Секрет сессии по умолчанию — не ошибка запуска, но на стенде это дыра,
    # и увидеть её должен администратор, а не проверяющий.
    default_secret = settings.session_secret.startswith("dev-secret")
    states.append(
        ServiceState(
            name="Секрет сессии",
            ok=not default_secret,
            detail="заменён" if not default_secret else "стоит значение по умолчанию — поменяйте SESSION_SECRET",
        )
    )
    return states


class BackupOut(BaseModel):
    name: str
    size_bytes: int
    at: datetime


@router.get("/backups", response_model=list[BackupOut])
async def backups(request: Request) -> list[BackupOut]:
    require(request, Role.ADMIN)
    return [BackupOut(**item) for item in await run_in_threadpool(backup_service.listing)]


def _safe_backup_error(exc: backup_service.BackupError) -> str:
    """Never echo DATABASE_URL/its password from backup diagnostics to HTTP."""
    message = str(exc)
    dsn = get_settings().database_url
    if dsn:
        message = message.replace(dsn, "[DATABASE_URL скрыт]")
        match = re.search(r"://[^:]+:([^@]+)@", dsn)
        if match and match.group(1):
            message = message.replace(match.group(1), "[пароль скрыт]")
    return message


@router.post("/backups", response_model=BackupOut, status_code=201)
async def make_backup(request: Request) -> BackupOut:
    """Копия прямо сейчас. Расписание — отдельно, в `scripts/backup.py`:
    кнопка нужна перед занятием, расписание — чтобы о нём не вспоминали."""
    who = require(request, Role.ADMIN)
    try:
        # pg_dump may run for two minutes; never block the event loop for it.
        created = await run_in_threadpool(backup_service.create)
    except backup_service.BackupError as exc:
        detail = _safe_backup_error(exc)
        await audit(who.login, who.role.value, "backup.failed", detail=detail)
        raise HTTPException(status_code=503, detail=detail) from exc
    await audit(who.login, who.role.value, "backup.create", created["name"])
    return BackupOut(**created)
