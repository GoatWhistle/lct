"""Сборка приложения. Роутеры подключаются по мере готовности — см. tasks/."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Прогрев моделей и валидация сценариев — карточки lct-04 и lct-06.
    app.state.models_ready = False
    yield


app = FastAPI(title="Учебный симулятор занятия для системы 112", lifespan=lifespan)


@app.get("/api/health")
async def health() -> dict:
    """Готовность стенда. Фронт показывает экран «модели прогреваются»."""
    settings = get_settings()
    return {
        "status": "ok",
        "models_ready": getattr(app.state, "models_ready", False),
        "offline": settings.offline,
    }
