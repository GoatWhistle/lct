"""Вход, роли и аудит.

ТЗ называет три роли поимённо — администратор, преподаватель, обучающийся —
и требует разграничения доступа, аудита действий и хранения журналов
безопасности (docs/spec/GAP.md#1-аутентификации-нет-вообще). До этой карточки
входа не было вовсе: экраны открывались ссылкой с номером занятия.

Решения, принятые заранее и не переоткрываемые (tasks/lct-23-auth-roles.md):

* пароль — `argon2`, не bcrypt;
* сессия — подписанная cookie через `SessionMiddleware`, не JWT и не таблица
  сессий: состояние занятия и так живёт в памяти одного воркера, лишний поход
  в базу на каждый запрос не нужен;
* на сокетах роль читается из cookie в момент рукопожатия — отдельного
  протокола авторизации в канале нет.
"""

import asyncio
import hashlib
import logging
import secrets
import weakref
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import get_settings
from app.db.base import get_sessionmaker
from app.db.models import AuditLog, User
from app.domain.roles import Role

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])
DEMO_TRAINEE_ID = UUID("00000000-0000-4000-8000-000000000112")

_hasher = PasswordHasher()
# Cookie is signed and may safely survive a backend restart. Marker changes
# only together with SESSION_SECRET; account edits are revoked independently
# through the persistent auth_version loaded below.
_INSTANCE = hashlib.sha256(
    f"lct-auth-instance:{get_settings().session_secret}".encode()
).hexdigest()
_generations: dict[str, int] = {}
_active_sockets: dict[str, weakref.WeakKeyDictionary] = {}


async def _close_revoked(ws: WebSocket) -> None:
    try:
        await ws.close(code=1008, reason="Учётная запись изменена: войдите снова")
    except (RuntimeError, OSError):
        # The peer may already have disconnected; revocation still stands.
        pass


def prime_generations(values: dict[str, int]) -> None:
    """Загрузить версии полномочий из БД при старте нового процесса."""
    _generations.clear()
    _generations.update(values)


async def load_generations() -> None:
    async with get_sessionmaker()() as db:
        rows = (await db.execute(select(User.login, User.auth_version))).all()
    prime_generations({login: version for login, version in rows})


def invalidate_login(login: str, version: int | None = None) -> None:
    """Revoke previously issued cookies after account/role/password changes."""
    _generations[login] = version if version is not None else _generations.get(login, 0) + 1
    for ws, loop in list(_active_sockets.get(login, {}).items()):
        try:
            if not loop.is_closed():
                loop.call_soon_threadsafe(lambda socket=ws: asyncio.create_task(_close_revoked(socket)))
        except RuntimeError:
            pass  # loop closed between the check and scheduling


def _session_principal(session: dict) -> "Principal | None":
    data = session.get("principal")
    if not isinstance(data, dict):
        return None
    login = data.get("login")
    if not isinstance(login, str):
        return None
    if session.get("auth_instance") != _INSTANCE:
        return None
    if session.get("auth_generation") != _generations.get(login, 0):
        return None
    try:
        return Principal.model_validate(data)
    except Exception:  # malformed signed cookie: reject, do not 500
        log.warning("неверный формат principal в cookie")
        return None


def _issue_session(request: Request, who: "Principal") -> None:
    request.session["principal"] = who.model_dump(mode="json")
    request.session["auth_instance"] = _INSTANCE
    request.session["auth_generation"] = _generations.get(who.login, 0)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:  # noqa: BLE001 — битый хеш не должен пускать в систему
        log.exception("проверка пароля не удалась")
        return False


class Principal(BaseModel):
    """Кто действует. Cookie stores a snapshot, revoked on account edits."""

    login: str
    full_name: str
    role: Role
    service: str | None = None
    trainee_id: UUID | None = None

    @property
    def is_staff(self) -> bool:
        return self.role in (Role.ADMIN, Role.INSTRUCTOR)


class LoginIn(BaseModel):
    login: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


def _demo_local(request: Request) -> None:
    # При ошибочном bind 0.0.0.0 удалённый клиент всё равно не получит cookie.
    if request.client is None or request.client.host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(status_code=403, detail="demo_local_only")


def _demo_principal(role: Role) -> Principal:
    return Principal(
        login="demo-trainee" if role is Role.TRAINEE else "demo-instructor",
        full_name="Демо-курсант" if role is Role.TRAINEE else "Демо-преподаватель",
        role=role,
        trainee_id=DEMO_TRAINEE_ID if role is Role.TRAINEE else None,
    )


def current(request: Request) -> Principal | None:
    return _session_principal(request.session)


def principal_of(websocket: WebSocket) -> Principal | None:
    """Роль на сокете — из той же cookie, что и на HTTP.

    `WebSocket.session` доступен, потому что `SessionMiddleware` стоит до
    роутера: значит роль известна до входа в цикл приёма сообщений.
    """
    who = _session_principal(websocket.session) if "session" in websocket.scope else None
    if who is not None:
        _active_sockets.setdefault(who.login, weakref.WeakKeyDictionary())[websocket] = asyncio.get_running_loop()
    return who


def require(request: Request, *roles: Role) -> Principal:
    """Принципал нужной роли или отказ. Единственная точка проверки на HTTP."""
    who = current(request)
    if who is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    if roles and who.role not in roles:
        raise HTTPException(status_code=403, detail="forbidden")
    return who


async def audit(
    actor: str, role: str, action: str, object_id: str | None = None, detail: str = ""
) -> None:
    """Запись в журнал. Аудит не должен ронять действие: если база недоступна,
    занятие продолжается, а пропуск виден в логе."""
    if get_settings().demo_no_db:
        return  # в явном demo-режиме запись и долговременный аудит недоступны
    try:
        async with get_sessionmaker()() as db:
            db.add(
                AuditLog(
                    actor=actor, role=role, action=action, object_id=object_id, detail=detail[:2000]
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("аудит: запись %s не удалась", action)


@router.post("/login")
async def login(payload: LoginIn, request: Request) -> dict:
    if get_settings().demo_no_db:
        _demo_local(request)
        demo_roles = {"demo-instructor": Role.INSTRUCTOR, "demo-trainee": Role.TRAINEE}
        role = demo_roles.get(payload.login)
        if role is None or not secrets.compare_digest(payload.password, "demo"):
            raise HTTPException(status_code=401, detail="bad_credentials")
        who = _demo_principal(role)
        _issue_session(request, who)
        return who.model_dump(mode="json")

    async with get_sessionmaker()() as db:
        user = await db.scalar(select(User).where(User.login == payload.login))

    # Одинаковый ответ на неизвестный логин и неверный пароль: иначе форма
    # входа превращается в список действующих учётных записей.
    if user is None or not verify_password(user.password_hash, payload.password):
        # Не записываем пароль, IP либо факт существования учётной записи.
        # Логин нужен администратору для расследования перебора; ограничиваем
        # длину до размера поля AuditLog.actor.
        await audit(payload.login[:80], "unknown", "login.failed")
        raise HTTPException(status_code=401, detail="bad_credentials")
    if user.blocked:
        await audit(user.login, user.role, "login.blocked")
        raise HTTPException(status_code=403, detail="blocked")

    _generations[user.login] = user.auth_version

    who = Principal(
        login=user.login,
        full_name=user.full_name,
        role=Role(user.role),
        service=user.service,
        trainee_id=user.trainee_id,
    )
    _issue_session(request, who)
    await audit(who.login, who.role.value, "login")
    return who.model_dump(mode="json")


@router.post("/logout")
async def logout(request: Request) -> dict:
    who = current(request)
    request.session.clear()
    if who:
        await audit(who.login, who.role.value, "logout")
    return {"ok": True}


@router.get("/me")
async def me(request: Request) -> dict:
    who = current(request)
    if who is None:
        raise HTTPException(status_code=401, detail="not_authenticated")
    return who.model_dump(mode="json")


@router.post("/dev-token")
async def dev_token(request: Request, role: Role = Role.INSTRUCTOR) -> dict:
    """Вход без пароля для наших же инструментов.

    `make lesson` и тесты открывают сокеты напрямую и после включения ролей
    сломались бы все разом. Точка живёт за флагом `dev_auth_bypass`, по
    умолчанию выключенным: в рабочем стенде она отвечает 404, а не 401 —
    выключенной функции не должно быть видно вовсе.
    """
    settings = get_settings()
    if not settings.dev_auth_bypass:
        raise HTTPException(status_code=404, detail="not_found")
    if role is not Role.INSTRUCTOR and not settings.demo_no_db:
        raise HTTPException(status_code=404, detail="not_found")
    if settings.demo_no_db:
        _demo_local(request)
        if role not in {Role.INSTRUCTOR, Role.TRAINEE}:
            raise HTTPException(status_code=403, detail="demo_role_forbidden")
    who = _demo_principal(role) if settings.demo_no_db else Principal(
        login="dev", full_name="Разработка", role=Role.INSTRUCTOR
    )
    _issue_session(request, who)
    return who.model_dump(mode="json")
