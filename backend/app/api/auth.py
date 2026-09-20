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

import logging
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel
from sqlalchemy import select

from app.config import get_settings
from app.db.base import get_sessionmaker
from app.db.models import AuditLog, User
from app.domain.roles import Role

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

_hasher = PasswordHasher()


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
    """Кто действует. Хранится в cookie целиком: ходить в базу за ролью
    на каждый запрос незачем, а меняется она правкой учётной записи."""

    login: str
    full_name: str
    role: Role
    service: str | None = None
    trainee_id: UUID | None = None

    @property
    def is_staff(self) -> bool:
        return self.role in (Role.ADMIN, Role.INSTRUCTOR)


class LoginIn(BaseModel):
    login: str
    password: str


def current(request: Request) -> Principal | None:
    data = request.session.get("principal")
    return Principal.model_validate(data) if data else None


def principal_of(websocket: WebSocket) -> Principal | None:
    """Роль на сокете — из той же cookie, что и на HTTP.

    `WebSocket.session` доступен, потому что `SessionMiddleware` стоит до
    роутера: значит роль известна до входа в цикл приёма сообщений.
    """
    data = websocket.session.get("principal") if "session" in websocket.scope else None
    return Principal.model_validate(data) if data else None


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
    async with get_sessionmaker()() as db:
        user = await db.scalar(select(User).where(User.login == payload.login))

    # Одинаковый ответ на неизвестный логин и неверный пароль: иначе форма
    # входа превращается в список действующих учётных записей.
    if user is None or not verify_password(user.password_hash, payload.password):
        raise HTTPException(status_code=401, detail="bad_credentials")
    if user.blocked:
        raise HTTPException(status_code=403, detail="blocked")

    who = Principal(
        login=user.login,
        full_name=user.full_name,
        role=Role(user.role),
        service=user.service,
        trainee_id=user.trainee_id,
    )
    request.session["principal"] = who.model_dump(mode="json")
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
async def dev_token(request: Request) -> dict:
    """Вход без пароля для наших же инструментов.

    `make lesson` и тесты открывают сокеты напрямую и после включения ролей
    сломались бы все разом. Точка живёт за флагом `dev_auth_bypass`, по
    умолчанию выключенным: в рабочем стенде она отвечает 404, а не 401 —
    выключенной функции не должно быть видно вовсе.
    """
    if not get_settings().dev_auth_bypass:
        raise HTTPException(status_code=404, detail="not_found")
    who = Principal(login="dev", full_name="Разработка", role=Role.INSTRUCTOR)
    request.session["principal"] = who.model_dump(mode="json")
    return who.model_dump(mode="json")
