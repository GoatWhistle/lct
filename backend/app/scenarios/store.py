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
_demo_drafts: dict[str, ScenarioRow] = {}
_demo_archived: dict[str, Scenario] = {}
_demo_scenario_owners: dict[str, str] = {}
_demo_archived_owners: dict[str, str] = {}


def reset_demo_drafts() -> None:
    """Черновики и архив demo-lite живут только в текущем процессе."""
    _demo_drafts.clear()
    _demo_archived.clear()
    _demo_scenario_owners.clear()
    _demo_archived_owners.clear()


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
        row.owner_login = None
    await db.commit()
    return len(scenarios)


async def restore_published(db: AsyncSession) -> int:
    """После старта вернуть утверждённые преподавателем сценарии из БД.

    Вызвать после ``load_from_disk``. Базовую библиотеку не перезаписываем:
    её источником остаются проверенные YAML из репозитория.
    """
    rows = await db.scalars(select(ScenarioRow).where(
        ScenarioRow.status.in_(["published", "archived"])
    ))
    delta = 0
    for row in rows:
        if row.status == "archived":
            if row.owner_login:
                _demo_scenario_owners.pop(row.id, None)
                _demo_archived_owners[row.id] = row.owner_login
            if _library.pop(row.id, None) is not None:
                delta -= 1
            continue
        if row.owner_login:
            _demo_scenario_owners[row.id] = row.owner_login
        if row.id in _library:
            continue
        _library[row.id] = Scenario.model_validate(row.body)
        delta += 1
    return delta


async def owned_scenario_ids(db: AsyncSession | None, owner_login: str) -> set[str]:
    """IDs the current instructor may edit/archive; base and legacy rows are read-only."""
    if db is None:
        return {
            scenario_id for scenario_id, owner in _demo_scenario_owners.items()
            if owner == owner_login and scenario_id in _library
        }
    rows = await db.scalars(select(ScenarioRow.id).where(
        ScenarioRow.owner_login == owner_login,
        ScenarioRow.status == "published",
    ))
    values = set(rows)
    # Test doubles and alternate SQLAlchemy result wrappers may return rows
    # for a scalar selection; normalize them without leaking owner data.
    return {value if isinstance(value, str) else value.id for value in values}


async def archive(
    db: AsyncSession | None, scenario_id: str, *, owner_login: str | None = None
) -> Scenario | None:
    """Скрыть опубликованный сценарий без удаления истории и внешних ключей."""
    scenario = _library.get(scenario_id)
    if scenario is None:
        return None
    if db is None:
        owner = _demo_scenario_owners.get(scenario_id)
        if owner_login is not None and owner != owner_login:
            return None
        _demo_archived[scenario_id] = scenario
        if owner is not None:
            _demo_archived_owners[scenario_id] = owner
        _demo_scenario_owners.pop(scenario_id, None)
    else:
        row = await db.get(ScenarioRow, scenario_id)
        if owner_login is not None and (row is None or row.owner_login != owner_login):
            return None
        if row is None:
            row = ScenarioRow(
                id=scenario.id,
                title=scenario.title,
                incident_type=scenario.type.value,
                level=scenario.level.value,
                topics=scenario.topics,
                modes=scenario.modes,
                body=scenario.model_dump(mode="json"),
            )
            db.add(row)
        row.status = "archived"
        await db.commit()
    _library.pop(scenario_id, None)
    return scenario


async def restore_archived(
    db: AsyncSession | None, scenario_id: str, *, owner_login: str | None = None
) -> Scenario | None:
    """Вернуть мягко удалённый сценарий в библиотеку назначения."""
    if db is None:
        owner = _demo_archived_owners.get(scenario_id)
        if owner_login is not None and owner != owner_login:
            return None
        scenario = _demo_archived.pop(scenario_id, None)
        if scenario is not None and owner is not None:
            _demo_scenario_owners[scenario_id] = owner
        _demo_archived_owners.pop(scenario_id, None)
    else:
        row = await db.get(ScenarioRow, scenario_id)
        if (row is None or row.status != "archived"
                or owner_login is not None and row.owner_login != owner_login):
            return None
        # В опубликованном body уже лежат вычисленные поля ground_truth (ЕКП,
        # ДДС, список оповещения). Редакторская validate предназначена для
        # черновика до вычисления и закономерно запрещает такие поля.
        scenario = Scenario.model_validate(row.body)
        row.status = "published"
        await db.commit()
    if scenario is None:
        return None
    publish(scenario)
    return scenario


async def create_draft(
    db: AsyncSession | None, *, source: Scenario, title: str | None = None,
    proposal: dict | None = None, full_proposal: dict | None = None,
    owner_login: str | None = None,
) -> ScenarioRow:
    if proposal is not None and full_proposal is not None:
        raise ValueError("нельзя одновременно передать вариацию и полный сюжет")
    prefix = "ai-full" if full_proposal is not None else "ai" if proposal is not None else "draft"
    draft_id = f"{prefix}-{uuid4().hex}"
    if full_proposal is not None:
        from app.scenarios.generation import full_proposal_body

        body = full_proposal_body(source, draft_id, full_proposal)
    elif proposal is None:
        body = editor.template_copy(source, draft_id, title)
    else:
        from app.scenarios.generation import proposal_body

        body = proposal_body(source, draft_id, proposal)
    row = ScenarioRow(
        id=draft_id,
        title=body["title"],
        incident_type=body["type"],
        level=body["level"],
        topics=body["topics"],
        modes=body["modes"],
        status="draft",
        owner_login=owner_login,
        body=body,
    )
    if db is None:
        _demo_drafts[row.id] = row
    else:
        db.add(row)
        await db.commit()
    return row


async def draft(
    db: AsyncSession | None, scenario_id: str, *, owner_login: str | None = None
) -> ScenarioRow | None:
    row = _demo_drafts.get(scenario_id) if db is None else await db.get(ScenarioRow, scenario_id)
    if row is None or row.status != "draft":
        return None
    if owner_login is not None and row.owner_login != owner_login:
        return None
    return row


async def update_draft(db: AsyncSession | None, row: ScenarioRow, patch: dict) -> ScenarioRow:
    row.body = editor.merge_patch(row.body, patch)
    row.title = str(row.body.get("title") or "")[:200]
    if db is not None:
        await db.commit()
    return row


async def revise_draft(db: AsyncSession | None, row: ScenarioRow, proposal: dict) -> ScenarioRow:
    """Заменить сюжетную версию того же черновика после комментария преподавателя."""
    from app.scenarios.generation import proposal_body

    source = editor.validate(row.body)
    row.body = proposal_body(source, row.id, proposal)
    row.title = row.body["title"]
    row.incident_type = row.body["type"]
    row.level = row.body["level"]
    row.topics = row.body["topics"]
    row.modes = row.body["modes"]
    if db is not None:
        await db.commit()
    return row


async def approve_draft(db: AsyncSession | None, row: ScenarioRow) -> Scenario:
    scenario = editor.validate(row.body)
    row.title = scenario.title
    row.incident_type = scenario.type.value
    row.level = scenario.level.value
    row.topics = scenario.topics
    row.modes = scenario.modes
    row.body = scenario.model_dump(mode="json")
    row.status = "published"
    if db is None:
        _demo_drafts.pop(row.id, None)
        if row.owner_login is not None:
            _demo_scenario_owners[row.id] = row.owner_login
    else:
        await db.commit()
    publish(scenario)
    return scenario
