"""Подключение к Postgres. Асинхронный движок, одна фабрика сессий на процесс."""

from collections.abc import AsyncIterator

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    """Общая база моделей. Alembic сравнивает миграции с `Base.metadata`."""


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


def reset() -> None:
    """Забыть движок и фабрику.

    Нужно тестам: каждый `TestClient` поднимает свой событийный цикл, а пул
    соединений привязан к тому, в котором создан. Переиспользование даёт
    «Future attached to a different loop» — причём не сразу, а на втором
    обращении к базе, и выглядит это как случайно падающий тест.
    """
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """Зависимость FastAPI: сессия на запрос, коммит явный."""
    if get_settings().demo_no_db:
        raise HTTPException(status_code=503, detail="database_disabled_demo")
    async with get_sessionmaker()() as session:
        yield session
