"""Журнал сессий. Тесты идут против живой базы из `make dev`;
если её нет — пропускаются, чтобы `make test` оставался запускаемым везде.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import repo
from app.db.models import Group, Scenario, Session, Trainee


@pytest.fixture
async def db():
    """Свой движок на каждый тест: глобальный в app.db.base кэшируется и
    привязывается к первому событийному циклу, а pytest даёт новый на каждый тест."""
    engine = create_async_engine(get_settings().database_url, poolclass=None)
    try:
        async with engine.connect() as probe:
            await probe.execute(text("select 1"))
    except Exception as exc:  # noqa: BLE001 — важен факт недоступности, не причина
        await engine.dispose()
        pytest.skip(f"Postgres недоступен ({type(exc).__name__}) — подними `make dev`")

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def scenario(db):
    row = Scenario(
        id=f"test-{uuid4().hex[:8]}",
        title="Тестовый",
        incident_type="fire",
        level="L1",
        topics=[],
        modes=["training"],
        body={},
    )
    db.add(row)
    await db.commit()
    yield row
    await db.execute(delete(Session).where(Session.scenario_id == row.id))
    await db.execute(delete(Scenario).where(Scenario.id == row.id))
    await db.commit()


async def test_attempts_count_up(db, scenario):
    """Дельта попыток — измеримый цикл: разбор → повтор → дельта."""
    group = await repo.ensure_group(db, f"группа-{uuid4().hex[:6]}")
    trainee = await repo.ensure_trainee(db, f"курсант-{uuid4().hex[:6]}", group)

    first = await repo.create_session(
        db, scenario_id=scenario.id, mode="training", trainee_id=trainee.id, group_id=group.id
    )
    second = await repo.create_session(
        db, scenario_id=scenario.id, mode="exam", trainee_id=trainee.id, group_id=group.id
    )

    assert (first.attempt, second.attempt) == (1, 2)
    assert second.group_id == group.id, "группа размечается с первой миграции"

    await db.execute(delete(Trainee).where(Trainee.id == trainee.id))
    await db.execute(delete(Group).where(Group.id == group.id))
    await db.commit()


async def test_transcript_keeps_order_and_anchors(db, scenario):
    session = await repo.create_session(db, scenario_id=scenario.id, mode="training")
    at = datetime.now(timezone.utc)

    await repo.append_utterance(
        db, session_id=session.id, ref="u1", speaker="caller", text="Алло! Горим!", at=at, mood="panic"
    )
    await repo.append_utterance(
        db, session_id=session.id, ref="u2", speaker="operator", text="Назовите адрес", at=at
    )

    rows = await repo.transcript(db, session.id)
    assert [row.ref for row in rows] == ["u1", "u2"]
    assert rows[0].mood == "panic"


async def test_history_filters_by_mode(db, scenario):
    await repo.create_session(db, scenario_id=scenario.id, mode="training")
    await repo.create_session(db, scenario_id=scenario.id, mode="exam")

    exams = await repo.history(db, mode="exam")
    assert exams, "контрольные сессии не нашлись"
    assert all(row.mode == "exam" for row in exams)


async def test_hints_are_logged(db, scenario):
    """Счёт подсказок — материал разбора, а не вычитаемое из баллов,
    но он обязан быть в журнале."""
    session = await repo.create_session(db, scenario_id=scenario.id, mode="training")
    await repo.record_hint(
        db,
        session_id=session.id,
        checklist_id="q_people",
        question="Есть ли люди в помещении?",
        at=datetime.now(timezone.utc),
    )
    count = await db.scalar(
        text("select count(*) from hint_uses where session_id = :sid").bindparams(sid=session.id)
    )
    assert count == 1
