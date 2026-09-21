"""Библиотека в памяти процесса и её заливка в БД.

В памяти живёт то, что читает голосовой контур и оценка; в БД — то, из чего
преподаватель выбирает сценарий и что переживает перезапуск.
"""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Scenario as ScenarioRow
from app.scenarios.loader import load_library
from app.scenarios.schema import Scenario

_library: dict[str, Scenario] = {}


def set_library(scenarios: list[Scenario]) -> None:
    _library.clear()
    _library.update({scenario.id: scenario for scenario in scenarios})


def get(scenario_id: str) -> Scenario | None:
    return _library.get(scenario_id)


def all_scenarios() -> list[Scenario]:
    return list(_library.values())


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
