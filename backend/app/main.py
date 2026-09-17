"""Сборка приложения. Роутеры подключаются по мере готовности — см. tasks/."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pathlib import Path

from app.api.http import scenarios as scenarios_api
from app.api.http import sessions
from app.api.ws import call as call_ws
from app.api.ws import control as control_ws
from app.api.ws import observe as observe_ws
from app.config import get_settings
from app.db.base import get_sessionmaker
from app.dialog.runtime import get_embedder
from app.voice.models import get_voice_models
from app.scenarios import store
from app.session.hub import hub
from app.session.journal import DbJournal
from app.scenarios.loader import ScenarioError


LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"

# uvicorn настраивает только собственные логгеры: без этого INFO из модулей
# приложения («получено N кадров», замеры задержки голоса) молча теряется,
# а до лога доходят одни предупреждения. Чужие библиотеки — от WARNING.
logging.basicConfig(level=logging.WARNING, format="%(levelname)-8s %(name)s: %(message)s")
logging.getLogger("app").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Библиотека проверяется на старте целиком: сломанный сценарий, найденный
    # посреди занятия, — сценарий, которого не должно случиться.
    try:
        loaded = store.load_from_disk(LIBRARY)
    except ScenarioError as exc:
        raise RuntimeError(f"библиотека сценариев не прошла проверку: {exc}") from exc
    app.state.scenarios_loaded = len(loaded)

    # Журнал: всё, что не записано, для оценки не существует.
    hub.journal = DbJournal(get_sessionmaker())

    # Эмбеддинги для слот-автомата — грузятся один раз, до первого занятия.
    app.state.embeddings_ready = get_embedder() is not None

    # Модели речи: ~5 секунд на старте стенда вместо паузы на первом звонке.
    app.state.models_ready = get_voice_models() is not None
    yield

    await hub.shutdown()
    for state in list(hub._sessions.values()):
        if state.voice is not None:
            await state.voice.close()


app = FastAPI(title="Учебный симулятор занятия для системы 112", lifespan=lifespan)
app.include_router(sessions.router)
app.include_router(scenarios_api.router)
app.include_router(call_ws.router)
app.include_router(observe_ws.router)
app.include_router(control_ws.router)


@app.get("/api/health")
async def health() -> dict:
    """Готовность стенда. Фронт показывает экран «модели прогреваются»."""
    settings = get_settings()
    return {
        "status": "ok",
        "models_ready": getattr(app.state, "models_ready", False),
        "scenarios_loaded": getattr(app.state, "scenarios_loaded", 0),
        "embeddings_ready": getattr(app.state, "embeddings_ready", False),
        "offline": settings.offline,
    }
