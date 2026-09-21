"""Библиотека в памяти процесса и её заливка в БД.

В памяти живёт то, что читает голосовой контур и оценка; в БД — то, из чего
преподаватель выбирает сценарий и что переживает перезапуск.
"""

from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Scenario as ScenarioRow
from app.scenarios.loader import load_library
from app.scenarios.schema import Scenario
from app.scenarios import editor

_library: dict[str, Scenario] = {}


def set_library(scenarios: list[Scenario]) -> None:
    _library.clear()
    _library.update({scenario.id: scenario for scenario in scenarios})


def get(scenario_id: str) -> Scenario | None:
    return _library.get(scenario_id)


def all_scenarios() -> list[Scenario]:
    return list(_library.values())


def publish(scenario: Scenario) -> None:
    """Сделать утверждённую версию доступной запуску занятия немедленно."""
    _library[scenario.id] = scenario


def load_from_disk(root: Path) -> list[Scenario]:
    scenarios = load_library(root)
    set_library(scenarios)
    return scenarios


async def seed(db: AsyncSession, scenarios: list[Scenario]) -> int:
    """Залить библиотеку в БД. Повторный запуск обновляет, а не дублирует."""
    for scenario in scenarios:
        row = await db.scalar(select(ScenarioRow).where(ScenarioRow.id == scenario.id))
        payload = scenario.model_dump(mode="json")
        if row is None:
            row = ScenarioRow(id=scenario.id)
            db.add(row)
        row.title = scenario.title
        row.incident_type = scenario.type.value
        row.level = scenario.level.value
        row.topics = scenario.topics
        row.modes = scenario.modes
        row.body = payload
    await db.commit()
    return len(scenarios)


async def restore_published(db: AsyncSession) -> int:
    """После старта вернуть утверждённые преподавателем сценарии из БД.

    Вызвать после ``load_from_disk``. Базовую библиотеку не перезаписываем:
    её источником остаются проверенные YAML из репозитория.
    """
    rows = await db.scalars(select(ScenarioRow).where(ScenarioRow.status == "published"))
    restored = 0
    for row in rows:
        if row.id in _library:
            continue
        _library[row.id] = Scenario.model_validate(row.body)
        restored += 1
    return restored


async def create_draft(
    db: AsyncSession, *, source: Scenario, title: str | None = None
) -> ScenarioRow:
    draft_id = f"draft-{uuid4().hex}"
    body = editor.template_copy(source, draft_id, title)
    row = ScenarioRow(
        id=draft_id,
        title=body["title"],
        incident_type=body["type"],
        level=body["level"],
        topics=body["topics"],
        modes=body["modes"],
        status="draft",
        body=body,
    )
    db.add(row)
    await db.commit()
    return row


async def draft(db: AsyncSession, scenario_id: str) -> ScenarioRow | None:
    row = await db.get(ScenarioRow, scenario_id)
    return row if row is not None and row.status == "draft" else None


async def update_draft(db: AsyncSession, row: ScenarioRow, patch: dict) -> ScenarioRow:
    row.body = editor.merge_patch(row.body, patch)
    row.title = str(row.body.get("title") or "")[:200]
    await db.commit()
    return row


async def approve_draft(db: AsyncSession, row: ScenarioRow) -> Scenario:
    scenario = editor.validate(row.body)
    row.title = scenario.title
    row.incident_type = scenario.type.value
    row.level = scenario.level.value
    row.topics = scenario.topics
    row.modes = scenario.modes
    row.body = scenario.model_dump(mode="json")
    row.status = "published"
    await db.commit()
    publish(scenario)
    return scenario
