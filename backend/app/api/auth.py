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
import time
import weakref
from urllib.parse import urlsplit
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.db.base import get_sessionmaker
from app.db.models import AuditLog, Trainee, User
from app.directory import DirectoryDenied, DirectoryIdentity, DirectoryUnavailable
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
# Logins already revoked because their users row disappeared.
_vanished: set[str] = set()
AUTH_GENERATION_SYNC_SECONDS = 1.0
AUTH_GENERATION_MAX_AGE_SECONDS = 2.0
_generations_synced_at: float | None = None
# One lock per login this node has not synced yet, so a concurrent HTTP
# request and WS handshake for the same just-migrated login collapse into a
# single SELECT instead of one each (lct-42).
_lookup_locks: dict[str, asyncio.Lock] = {}


class _AuthStateUnavailable(Exception):
    """The one-shot lookup for a login unknown to this node could not reach PostgreSQL."""


async def _close_revoked(ws: WebSocket) -> None:
    try:
        await ws.close(code=1008, reason="Учётная запись изменена: войдите снова")
    except (RuntimeError, OSError, WebSocketDisconnect):
        # The peer may already have disconnected; revocation still stands.
        pass


async def _close_auth_state_unavailable(ws: WebSocket) -> None:
    try:
        await ws.close(code=1013, reason="Состояние доступа временно недоступно")
    except (RuntimeError, OSError, WebSocketDisconnect):
        pass


def _close_unverified_sockets() -> None:
    # Синтетический `dev` middleware пропускает без сверки поколения — и его
    # сокеты не зависят от синхронизации с БД, иначе стенд без базы рвёт
    # занятие через две секунды после старта.
    synthetic = {"dev"} if get_settings().dev_auth_bypass else set()
    for login, sockets in list(_active_sockets.items()):
        if login in synthetic:
            continue
        for ws, loop in list(sockets.items()):
            try:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(
                        lambda socket=ws: asyncio.create_task(
                            _close_auth_state_unavailable(socket)
                        )
                    )
            except RuntimeError:
                pass


def prime_generations(values: dict[str, int]) -> None:
    """Загрузить версии полномочий из БД при старте нового процесса."""
    global _generations_synced_at
    _generations.clear()
    _generations.update(values)
    _generations_synced_at = time.monotonic()


async def load_generations() -> None:
    async with get_sessionmaker()() as db:
        rows = (await db.execute(select(User.login, User.auth_version))).all()
    prime_generations({login: version for login, version in rows})


async def sync_generations() -> None:
    """Refresh shared account epochs and close sockets revoked on peer nodes."""
    async with get_sessionmaker()() as db:
        rows = (await db.execute(select(User.login, User.auth_version))).all()
    current = {login: version for login, version in rows}
    for login, version in current.items():
        previous = _generations.get(login)
        if previous is None:
            _generations[login] = version
        elif previous != version:
            invalidate_login(login, version)
    # Account deletion is not exposed by the application. Still close active
    # sockets if an operator removes one directly from the shared directory DB.
    # The local-only dev-token principal is synthetic, never stored in users;
    # a DB watcher must not revoke its in-memory generation during test/demo
    # flows that deliberately exercise account invalidation.
    # The revoked generation stays cached: dropping it would make the next
    # lookup fall back to 0 and re-accept cookies issued before the revocation.
    synthetic = {"dev"} if get_settings().dev_auth_bypass else set()
    _vanished.intersection_update(_generations.keys() - current.keys())
    for login in _generations.keys() - current.keys() - synthetic - _vanished:
        invalidate_login(login)
        _vanished.add(login)
    global _generations_synced_at
    _generations_synced_at = time.monotonic()


async def _resolve_unknown_login(login: str) -> int | None:
    """Look up a login absent from this node's cache without waiting a tick.

    A revoked login stays cached in `_generations` (see `_vanished` above),
    so absence from `_generations` unambiguously means "this node has not
    synced this login yet" — an unrecognized login is checked directly
    rather than treated as revoked. Returns the current epoch, or `None` if
    the login is not (or no longer) in `users`. Raises
    `_AuthStateUnavailable` if PostgreSQL cannot be reached; the caller
    fails closed.
    """
    lock = _lookup_locks.setdefault(login, asyncio.Lock())
    async with lock:
        cached = _generations.get(login)
        if cached is not None:
            return cached  # a concurrent request already resolved it
        try:
            async with get_sessionmaker()() as db:
                version = await db.scalar(
                    select(User.auth_version).where(User.login == login)
                )
        except Exception as exc:  # noqa: BLE001 — fail closed, not FORBIDDEN
            log.error("разовая проверка полномочий не удалась (%s)", type(exc).__name__)
            raise _AuthStateUnavailable from exc
        if version is None:
            return None
        _generations[login] = version
        return version


async def watch_generations() -> None:
    """Poll PostgreSQL once per node so remote logout/role changes close WS."""
    while True:
        try:
            async with asyncio.timeout(AUTH_GENERATION_MAX_AGE_SECONDS):
                await sync_generations()
        except Exception as exc:  # noqa: BLE001 — retry; authenticated requests fail closed
            log.error("не удалось синхронизировать версии полномочий (%s)",
                      type(exc).__name__)
            if (
                _generations_synced_at is None
                or time.monotonic() - _generations_synced_at > AUTH_GENERATION_MAX_AGE_SECONDS
            ):
                _close_unverified_sockets()
        await asyncio.sleep(AUTH_GENERATION_SYNC_SECONDS)


async def _send_auth_state_unavailable(scope, send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1013})
    else:
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"detail":"auth_state_unavailable"}',
        })


class AuthVersionMiddleware:
    """Check signed-cookie epochs against the fresh, DB-synchronized node cache."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http" and scope.get("path") in {
            "/api/health", "/api/auth/logout",
        }:
            # Liveness must remain observable and logout must always be able to
            # clear the browser cookie even while PostgreSQL is unreachable.
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        session = scope.get("session")
        data = session.get("principal") if isinstance(session, dict) else None
        login = data.get("login") if isinstance(data, dict) else None
        if (
            settings.demo_no_db
            or not isinstance(login, str)
            or login == "dev" and settings.dev_auth_bypass
            or session.get("auth_instance") != _INSTANCE
        ):
            await self.app(scope, receive, send)
            return

        synced_at = _generations_synced_at
        if (
            synced_at is None
            or time.monotonic() - synced_at > AUTH_GENERATION_MAX_AGE_SECONDS
        ):
            await _send_auth_state_unavailable(scope, send)
            return

        cookie_version = session.get("auth_generation")
        version = _generations.get(login)
        if version is None:
            try:
                version = await _resolve_unknown_login(login)
            except _AuthStateUnavailable:
                await _send_auth_state_unavailable(scope, send)
                return
        if version is None or cookie_version != version:
            if session is not None:
                session.clear()
            await self.app(scope, receive, send)
            return
        await self.app(scope, receive, send)


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
    except Exception as exc:  # noqa: BLE001 — битый хеш не должен пускать в систему
        # Do not echo malformed stored hash material in diagnostic tracebacks.
        log.error("проверка пароля не удалась (%s)", type(exc).__name__)
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


def websocket_origin_allowed(websocket: WebSocket) -> bool:
    """Reject browser cross-site WebSocket handshakes (CSWSH).

    Non-browser clients may omit Origin. Browser Origins must exactly match
    the external host and scheme; the bundled reverse proxies forward the
    original Host and scheme explicitly for this check.
    """
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    try:
        parsed_origin = urlsplit(origin)
        host = websocket.headers.get("x-forwarded-host") or websocket.headers.get("host")
        scheme = (
            websocket.headers.get("x-forwarded-proto")
            or {"ws": "http", "wss": "https"}.get(websocket.scope.get("scheme", ""), "")
        ).casefold()
        if not host or scheme not in {"http", "https"}:
            return False
        expected = urlsplit(f"{scheme}://{host}")
        if parsed_origin.scheme.casefold() != scheme:
            return False
        if (
            parsed_origin.username
            or parsed_origin.password
            or not parsed_origin.hostname
            or not expected.hostname
        ):
            return False
        origin_port = parsed_origin.port or (443 if scheme == "https" else 80)
        expected_port = expected.port or (443 if scheme == "https" else 80)
        return (
            parsed_origin.hostname.casefold() == expected.hostname.casefold()
            and origin_port == expected_port
            and parsed_origin.path in {"", "/"}
            and not parsed_origin.query
            and not parsed_origin.fragment
        )
    except ValueError:
        return False


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
) -> bool:
    """Best-effort audit write for actions that cannot be rolled back."""
    if get_settings().demo_no_db:
        return True  # явный demo-режим не обещает долговременное хранение
    try:
        async with get_sessionmaker()() as db:
            add_audit_entry(db, actor, role, action, object_id, detail)
            await db.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        # SQL traces can include audit detail and user-provided text.
        log.error("аудит: запись %s не удалась (%s)", action, type(exc).__name__)
        return False


async def audit_required(
    actor: str, role: str, action: str, object_id: str | None = None, detail: str = ""
) -> None:
    """Fail closed for authentication decisions that must be auditable."""
    written = await audit(actor, role, action, object_id, detail)
    # `is False` preserves simple third-party/test audit hooks that return None.
    if written is False:
        raise HTTPException(status_code=503, detail="audit_unavailable")


def add_audit_entry(
    db, actor: str, role: str, action: str, object_id: str | None = None, detail: str = ""
) -> None:
    """Добавить audit row к текущей транзакции, не коммитя отдельно.

    Для административных операций, где изменение без audit trail недопустимо,
    вызывающий код коммитит предметную запись и журнал одним commit.
    """
    db.add(AuditLog(
        actor=actor,
        role=role,
        action=action,
        object_id=object_id,
        detail=detail[:2000],
    ))


async def _directory_account(identity: DirectoryIdentity) -> User:
    """Just-in-time provision and sync one explicitly group-mapped account."""
    async with get_sessionmaker()() as db:
        by_login = await db.scalar(select(User).where(User.login == identity.login))
        by_subject = await db.scalar(
            select(User).where(User.directory_subject == identity.subject)
        )
        if by_login is not None and by_login.auth_provider != "ldap":
            raise DirectoryDenied("directory login conflicts with a local account")
        if by_login is not None and by_subject is not None and by_login.id != by_subject.id:
            raise DirectoryDenied("directory identity conflicts with an existing account")
        user = by_subject or by_login
        if user is not None and user.blocked:
            # Let the login endpoint record the blocked attempt with the same
            # audit path used for local accounts. Do not sync any account fields.
            return user
        if user is not None and user.directory_subject not in {None, identity.subject}:
            raise DirectoryDenied("directory login is bound to another identity")

        if user is None:
            trainee_id = None
            if identity.role is Role.TRAINEE:
                trainee = Trainee(name=identity.full_name)
                db.add(trainee)
                await db.flush()
                trainee_id = trainee.id
            user = User(
                login=identity.login,
                password_hash=hash_password(secrets.token_urlsafe(48)),
                full_name=identity.full_name,
                role=identity.role.value,
                service=identity.service,
                trainee_id=trainee_id,
                auth_provider="ldap",
                directory_subject=identity.subject,
                auth_version=0,
                blocked=False,
            )
            db.add(user)
            add_audit_entry(
                db,
                "system",
                "system",
                "user.provision.ldap",
                identity.login,
                f"role={identity.role.value}; service_assigned={identity.service is not None}",
            )
        else:
            if user.role != identity.role.value and identity.role is Role.TRAINEE and user.trainee_id is None:
                trainee = Trainee(name=identity.full_name)
                db.add(trainee)
                await db.flush()
                user.trainee_id = trainee.id
            changed = (
                user.full_name != identity.full_name
                or user.role != identity.role.value
                or user.service != identity.service
                or user.directory_subject != identity.subject
            )
            user.full_name = identity.full_name
            user.role = identity.role.value
            user.service = identity.service
            user.directory_subject = identity.subject
            if user.trainee_id is not None:
                trainee = await db.get(Trainee, user.trainee_id)
                if trainee is not None:
                    trainee.name = identity.full_name
            if changed:
                user.auth_version += 1
                add_audit_entry(
                    db,
                    "system",
                    "system",
                    "user.sync.ldap",
                    identity.login,
                    f"role={user.role}; service_assigned={user.service is not None}",
                )
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            # Concurrent first login or duplicate directory subject is denied;
            # the caller can retry after the account mapping is unambiguous.
            raise DirectoryDenied("directory account provisioning conflict") from exc
        await db.refresh(user)
        return user


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

    settings = get_settings()
    if user is None or user.auth_provider == "ldap":
        if not settings.ldap_enabled:
            raise HTTPException(status_code=401, detail="bad_credentials")
        from app.directory import authenticate

        try:
            identity = await authenticate(payload.login, payload.password)
            if identity is None:
                raise DirectoryDenied("unknown directory account")
            user = await _directory_account(identity)
        except DirectoryDenied as exc:
            await audit_required(payload.login[:80], "unknown", "login.failed")
            raise HTTPException(status_code=401, detail="bad_credentials") from exc
        except DirectoryUnavailable as exc:
            log.error("local directory unavailable: %s", exc)
            raise HTTPException(status_code=503, detail="directory_unavailable") from exc
    elif not verify_password(user.password_hash, payload.password):
        # Не записываем пароль, IP либо факт существования учётной записи.
        # Логин нужен администратору для расследования перебора; ограничиваем
        # длину до размера поля AuditLog.actor.
        await audit_required(payload.login[:80], "unknown", "login.failed")
        raise HTTPException(status_code=401, detail="bad_credentials")
    if user.blocked:
        await audit_required(user.login, user.role, "login.blocked")
        raise HTTPException(status_code=403, detail="blocked")

    if _generations.get(user.login) != user.auth_version:
        invalidate_login(user.login, user.auth_version)

    who = Principal(
        login=user.login,
        full_name=user.full_name,
        role=Role(user.role),
        service=user.service,
        trainee_id=user.trainee_id,
    )
    await audit_required(who.login, who.role.value, "login")
    _issue_session(request, who)
    return who.model_dump(mode="json")


@router.post("/logout")
async def logout(request: Request) -> dict:
    who = current(request)
    if who is None:
        request.session.clear()
        return {"ok": True}

    # Drop the browser cookie even if durable revocation is unavailable.
    request.session.clear()
    if get_settings().demo_no_db:
        invalidate_login(who.login)
        await audit(who.login, who.role.value, "logout")
        return {"ok": True}

    try:
        async with get_sessionmaker()() as db:
            user = await db.scalar(select(User).where(User.login == who.login).with_for_update())
            if user is not None:
                user.auth_version += 1
                version = user.auth_version
            else:
                version = _generations.get(who.login, 0) + 1
            add_audit_entry(db, who.login, who.role.value, "logout")
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — fail closed for revocation/audit
        log.error("выход: отзыв cookie и аудит не удалось сохранить (%s)",
                  type(exc).__name__)
        invalidate_login(who.login)
        raise HTTPException(status_code=503, detail="audit_unavailable") from exc
    invalidate_login(who.login, version)
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
