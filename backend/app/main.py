"""Сборка приложения. Роутеры подключаются по мере готовности — см. tasks/."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.sessions import SessionMiddleware

from app.api import auth
from app.api.http import admin as admin_api
from app.api.http import ekp as ekp_api
from app.api.http import groups as groups_api
from app.api.http import materials as materials_api
from app.api.http import scenario_submissions as scenario_submissions_api
from app.api.http import scenarios as scenarios_api
from app.api.http import sessions, trainees
from app.api.ws import call as call_ws
from app.api.ws import control as control_ws
from app.api.ws import observe as observe_ws
from app.api.ws import station as station_ws
from app.config import get_settings
from app.db.base import get_sessionmaker
from app.dialog.runtime import get_embedder
from app.monitoring import install_diagnostics
from app.scenarios import store
from app.scenarios.loader import ScenarioError
from app.session.hub import hub
from app.session.pg_store import PostgresSessionStore
from app.session.store import MemorySessionStore
from app.voice.models import get_voice_models

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"
#: Треть срока lease (`pg_store.LEASE_SECONDS`): два пропущенных оборота ещё не теряют занятие.
LEASE_SUPERVISOR_SECONDS = 5

# uvicorn настраивает только собственные логгеры: без этого INFO из модулей
# приложения («получено N кадров», замеры задержки голоса) молча теряется,
# а до лога доходят одни предупреждения. Чужие библиотеки — от WARNING.
logging.basicConfig(level=logging.WARNING, format="%(levelname)-8s %(name)s: %(message)s")
logging.getLogger("app").setLevel(logging.INFO)
install_diagnostics()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    try:
        settings.validate_deployment_security()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if settings.demo_no_db and not settings.dev_auth_bypass:
        raise RuntimeError("DEMO_NO_DB требует DEV_AUTH_BYPASS=true для локального входа")
    # Библиотека проверяется на старте целиком: сломанный сценарий, найденный
    # посреди занятия, — сценарий, которого не должно случиться.
    try:
        loaded = store.load_from_disk(LIBRARY)
    except ScenarioError as exc:
        raise RuntimeError(f"библиотека сценариев не прошла проверку: {exc}") from exc
    app.state.scenarios_loaded = len(loaded)
    if settings.demo_no_db:
        store.reset_demo_drafts()
        materials_api.reset_demo_materials()
        scenario_submissions_api.reset_demo_submissions()

    # Утверждённые преподавателем сценарии хранятся в БД и должны переживать
    # перезапуск процесса. При недоступной БД остаётся базовая YAML-библиотека.
    if not settings.demo_no_db:
        try:
            # БД может ещё подниматься или отсутствовать в unit-тесте. Не задерживаем
            # старт АРМ на минуту ради необязательной пользовательской библиотеки.
            async with asyncio.timeout(1):
                async with get_sessionmaker()() as db:
                    app.state.scenarios_loaded += await store.restore_published(db)
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            logging.getLogger(__name__).warning(
                "не удалось восстановить утверждённые сценарии из БД: %s", exc
            )
    else:
        logging.getLogger(__name__).warning(
            "DEMO_NO_DB: занятия и оценки живут только до перезапуска; хранилище в памяти"
        )

    if not settings.demo_no_db:
        try:
            async with asyncio.timeout(2):
                await auth.load_generations()
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            logging.getLogger(__name__).warning(
                "не удалось загрузить версии полномочий: %s", exc
            )

    # Хранилище: всё, что не записано, для оценки не существует. Без БД —
    # in-memory adapter, занятие живёт до перезапуска.
    hub.store = (
        MemorySessionStore() if settings.demo_no_db
        else PostgresSessionStore(get_sessionmaker(), node_id=settings.backend_node_id)
    )
    app.state.sessions_restored = 0
    try:
        async with asyncio.timeout(3):
            app.state.sessions_restored = await hub.restore()
        if app.state.sessions_restored:
            logging.getLogger(__name__).info(
                "восстановлено активных занятий: %d", app.state.sessions_restored
            )
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        logging.getLogger(__name__).warning(
            "не удалось восстановить активные занятия: %s", exc
        )

    # Lease держит только узел кластера: без node_id владелец один.
    lease_task = (
        asyncio.create_task(
            hub.supervise_lease(LEASE_SUPERVISOR_SECONDS),
            name="session-owner-lease-supervisor",
        )
        if hub.store.persistent and settings.backend_node_id else None
    )

    # Эмбеддинги для слот-автомата — грузятся один раз, до первого занятия.
    app.state.embeddings_ready = not settings.demo_no_db and get_embedder() is not None

    # Модели речи: ~5 секунд на старте стенда вместо паузы на первом звонке.
    app.state.models_ready = get_voice_models() is not None
    generation_watcher = (
        asyncio.create_task(auth.watch_generations(), name="auth-generation-sync")
        if not settings.demo_no_db else None
    )
    yield

    if generation_watcher is not None:
        generation_watcher.cancel()
        with suppress(asyncio.CancelledError):
            await generation_watcher

    if lease_task is not None:
        lease_task.cancel()
        with suppress(asyncio.CancelledError):
            await lease_task

    await hub.save_all()
    await hub.shutdown()


app = FastAPI(title="Учебный симулятор занятия для системы 112", lifespan=lifespan)
# Сессия ставится до роутеров: роль должна быть известна и на HTTP, и в момент
# рукопожатия сокета, иначе проверять её в канале будет нечем (app/api/auth.py).
app.add_middleware(
    auth.AuthVersionMiddleware,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=get_settings().session_secret,
    session_cookie="lct_session",
    https_only=get_settings().secure_cookies,
    max_age=12 * 60 * 60,  # смена занятий, не месяц
)
app.include_router(auth.router)
app.include_router(sessions.router)
app.include_router(scenarios_api.router)
app.include_router(scenario_submissions_api.router)
app.include_router(ekp_api.router)
app.include_router(groups_api.router)
app.include_router(materials_api.router)
app.include_router(admin_api.router)
app.include_router(trainees.router)
app.include_router(call_ws.router)
app.include_router(observe_ws.router)
app.include_router(control_ws.router)
app.include_router(station_ws.router)


@app.get("/api/health")
async def health() -> dict:
    """Готовность стенда. Фронт показывает экран «модели прогреваются»."""
    settings = get_settings()
    return {
        "status": "ok",
        "models_ready": getattr(app.state, "models_ready", False),
        "scenarios_loaded": getattr(app.state, "scenarios_loaded", 0),
        "embeddings_ready": getattr(app.state, "embeddings_ready", False),
        "sessions_restored": getattr(app.state, "sessions_restored", 0),
        "offline": settings.offline,
        "demo_no_db": settings.demo_no_db,
    }
