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
from datetime import datetime, timedelta, timezone
from uuid import UUID
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
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
from app.domain.roles import ROLE_LABELS, SCREENS, Role
from app.domain.timers import NORMATIVES
from app.dialog.llm import is_loopback_url
from app.monitoring import recent_events, sample_metrics
from app.session.hub import hub

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin", tags=["admin"])


def _configuration_xml() -> bytes:
    """Безопасный переносимый снимок конфигурации без паролей и ключей."""
    settings = get_settings()
    root = ET.Element("lctConfiguration", {"version": "1"})
    ET.SubElement(root, "platform", {
        "offline": str(settings.offline).lower(),
        "voiceEnabled": str(settings.voice_enabled).lower(),
        "secureCookies": str(settings.secure_cookies).lower(),
    })
    models = ET.SubElement(root, "localModels")
    ET.SubElement(models, "dialogue", {"name": settings.llm_model_caller})
    ET.SubElement(models, "russianControl", {
        "name": settings.llm_model_control,
        "grammarEnabled": str(settings.grammar_llm_enabled).lower(),
    })
    ET.SubElement(models, "speechToText", {
        "name": settings.stt_model,
        "enabled": str(settings.voice_enabled).lower(),
    })
    workstations = ET.SubElement(root, "workstations")
    for role, screens in SCREENS.items():
        workstation = ET.SubElement(workstations, "workstation", {
            "role": role.value, "label": ROLE_LABELS[role],
        })
        for path in screens:
            ET.SubElement(workstation, "screen", {"path": path})
    timers = ET.SubElement(root, "timerLimits")
    for code, normative in NORMATIVES.items():
        ET.SubElement(timers, "timer", {
            "code": code.value,
            "milliseconds": str(settings.limit_ms(code)),
            "defaultMilliseconds": str(normative.limit_ms),
        })
    reference = ekp.reference()
    ET.SubElement(root, "ekp", {
        "version": reference.version,
        "incidents": str(len(reference.incidents)),
    })
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


@router.get("/config.xml")
async def configuration_xml(request: Request) -> Response:
    require(request, Role.ADMIN)
    return Response(
        content=_configuration_xml(),
        media_type="application/xml",
        headers={"Content-Disposition": 'attachment; filename="lct-workstations.xml"'},
    )


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
    password: str = Field(min_length=8, max_length=1024, description="Пароль должен быть от 8 до 1024 символов")
    role: Role
    service: str | None = None


class UserPatch(BaseModel):
    """Что администратор меняет у существующей записи. Пароль — сбросом,
    а не чтением: старый он не видит и увидеть не может."""

    role: Role | None = None
    service: str | None = None
    blocked: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=1024)


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
    user.auth_version += 1
    await db.commit()
    invalidate_login(user.login, user.auth_version)
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
    offset: int = 0,
    db: AsyncSession = Depends(get_session),
) -> list[AuditOut]:
    """Журнал действий. Администратор его читает, но не правит: точки удаления
    или изменения записи здесь нет — ТЗ требует хранения, а не управления."""
    require(request, Role.ADMIN)
    query = (
        select(AuditLog)
        .order_by(AuditLog.at.desc(), AuditLog.id.desc())
        .limit(max(1, min(limit, 1001)))
        .offset(max(0, min(offset, 10_000_000)))
    )
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


class RuntimeMetrics(BaseModel):
    at: datetime
    uptime_seconds: float
    cpu_percent: float
    load_1m_percent: float | None
    cpu_cores: int
    rss_bytes: int | None
    memory_total_bytes: int | None
    memory_available_bytes: int | None
    disk_total_bytes: int
    disk_free_bytes: int
    threads: int
    active_sessions: int
    restored_sessions: int
    completed_sessions_24h: int


class DiagnosticEvent(BaseModel):
    at: datetime
    level: str
    source: str
    message: str


class FailureEvent(BaseModel):
    at: datetime
    actor: str
    action: str
    object_id: str | None
    detail: str


class DiagnosticReport(BaseModel):
    generated_at: datetime
    metrics: RuntimeMetrics
    recent_system_events: list[DiagnosticEvent]
    failed_actions_24h: list[FailureEvent]


async def _runtime_metrics(db: AsyncSession) -> RuntimeMetrics:
    from app.main import app

    raw = sample_metrics()
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    completed = await db.scalar(
        select(func.count()).select_from(SessionRow).where(SessionRow.ended_at >= since)
    )
    return RuntimeMetrics(
        **raw,
        active_sessions=sum(not item.ended for item in hub._sessions.values()),  # noqa: SLF001
        restored_sessions=getattr(app.state, "sessions_restored", 0),
        completed_sessions_24h=completed or 0,
    )


async def _diagnostic_report(db: AsyncSession) -> DiagnosticReport:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = await db.scalars(
        select(AuditLog)
        .where(AuditLog.at >= since, AuditLog.action.like("%.failed"))
        .order_by(AuditLog.at.desc())
        .limit(200)
    )
    return DiagnosticReport(
        generated_at=datetime.now(timezone.utc),
        metrics=await _runtime_metrics(db),
        recent_system_events=[DiagnosticEvent(**item) for item in recent_events(limit=100)],
        failed_actions_24h=[
            FailureEvent(
                at=row.at, actor=row.actor, action=row.action,
                object_id=row.object_id, detail=row.detail,
            )
            for row in rows
        ],
    )


@router.get("/diagnostics", response_model=DiagnosticReport)
async def diagnostics(
    request: Request, db: AsyncSession = Depends(get_session)
) -> DiagnosticReport:
    """Live load plus a bounded, redacted incident report for the admin."""
    require(request, Role.ADMIN)
    return await _diagnostic_report(db)


@router.get("/diagnostics.json")
async def download_diagnostics(
    request: Request, db: AsyncSession = Depends(get_session)
) -> JSONResponse:
    require(request, Role.ADMIN)
    report = await _diagnostic_report(db)
    return JSONResponse(
        jsonable_encoder(report),
        headers={"Content-Disposition": 'attachment; filename="lct-diagnostics.json"'},
    )


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
    metrics = sample_metrics()
    disk_free = metrics["disk_free_bytes"]
    disk_total = max(metrics["disk_total_bytes"], 1)
    disk_ok = disk_free >= 1024 ** 3 and disk_free / disk_total >= 0.05
    load = metrics["load_1m_percent"]
    states.append(ServiceState(
        name="Нагрузка backend",
        ok=disk_ok and (load is None or load < 100),
        detail=(
            f"CPU процесса {metrics['cpu_percent']:.1f}%; "
            + (f"нагрузка хоста {load:.1f}%; " if load is not None else "")
            + f"RAM процесса {(metrics['rss_bytes'] or 0) / 1024 ** 2:.0f} МБ; "
            + f"свободно на диске {disk_free / 1024 ** 3:.1f} ГБ"
        ),
    ))
    states.append(
        ServiceState(
            name="Эмбеддинги",
            ok=bool(getattr(app.state, "embeddings_ready", False)),
            detail="слот-автомат работает" if getattr(app.state, "embeddings_ready", False)
            else "нет модели: подсказки идут по порядку чек-листа",
        )
    )
    llm_configured = (
        is_loopback_url(
            settings.llm_base_url,
            allow_docker_host=settings.allow_docker_host_models,
        )
        if settings.offline or settings.llm_provider == "local"
        else bool(settings.llm_api_key and settings.llm_base_url)
    )
    states.append(
        ServiceState(
            name="Провайдер LLM",
            ok=llm_configured,
            detail=(f"локальный адрес разрешён: {settings.llm_base_url}"
                    if llm_configured else "не настроен: звонящий читает офлайн-таблицу"),
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
            detail=(
                f"активно {sum(not item.ended for item in hub._sessions.values())}; "
                f"восстановлено после запуска {getattr(app.state, 'sessions_restored', 0)}"
            ),  # noqa: SLF001 — реестр в памяти процесса
        )
    )
    try:
        copies = backup_service.listing()
    except backup_service.BackupError as exc:
        states.append(ServiceState(
            name="Резервное копирование",
            ok=False,
            detail=f"ошибка чтения копий: {exc}",
        ))
        copies = []
    if copies:
        latest = copies[0]
        age_seconds = max(0.0, (datetime.now(timezone.utc) - latest["at"]).total_seconds())
        allowed_age = max(60, settings.backup_interval_seconds) + max(
            60, settings.backup_retry_seconds
        )
        states.append(ServiceState(
            name="Резервное копирование",
            ok=age_seconds <= allowed_age,
            detail=(f"последняя копия {latest['name']}, "
                    f"{age_seconds / 3600:.1f} ч назад; хранится {len(copies)}"),
        ))
    else:
        states.append(ServiceState(
            name="Резервное копирование",
            ok=False,
            detail="успешных копий ещё нет",
        ))
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
    try:
        copies = await run_in_threadpool(backup_service.listing)
    except backup_service.BackupError as exc:
        detail = _safe_backup_error(exc)
        raise HTTPException(status_code=503, detail=detail) from exc
    return [BackupOut(**item) for item in copies]


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
