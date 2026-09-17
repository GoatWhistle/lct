"""Загрузчик библиотеки сценариев.

Проверяет **все** YAML на старте приложения и падает с внятным сообщением
при первом же нарушении: сломанный сценарий, найденный посреди занятия, —
сценарий, которого не должно случиться (docs/arch/BACKEND.md).
"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.domain.classifiers import DDS_BY_INCIDENT
from app.scenarios.schema import ChecklistItem, Scenario


class ScenarioError(Exception):
    """Ошибка библиотеки. Текст пишется для методиста, не для программиста."""


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path.name}: битый YAML — {exc}") from exc
    if not isinstance(data, dict):
        raise ScenarioError(f"{path.name}: ожидался словарь верхнего уровня")
    return data


def _merge_checklist(base: list[dict], local: list[dict]) -> list[ChecklistItem]:
    """Общий чек-лист по классификатору плюс локальные дополнения.

    Наследуются все пункты базы, локальные перекрывают одноимённые и добавляют
    свои. Пункт без `fact` допустим: «представьтесь» не добывает факт,
    но остаётся частью эталонного опроса.
    """
    merged: dict[str, dict] = {item["id"]: dict(item) for item in base}
    for item in local:
        merged.setdefault(item["id"], {}).update(item)
    return [ChecklistItem.model_validate(item) for item in merged.values()]


def _derive_ground_truth(scenario: Scenario) -> Scenario:
    """Эталон собирается кодом. Из YAML берутся только нормализованные
    адрес и число пострадавших — остальное перезаписывается."""
    hidden = {fact.id for fact in scenario.facts if fact.hidden}
    required = [
        item.fact
        for item in scenario.checklist
        if item.fact and item.fact not in hidden
    ]
    scenario.ground_truth.incident_type = scenario.type
    scenario.ground_truth.dds = DDS_BY_INCIDENT[scenario.type]
    scenario.ground_truth.required_facts = required
    return scenario


COMMON = "checklists/common.yaml"


def _common_not_questions(root: Path) -> list[str]:
    common = root / COMMON
    if not common.exists():
        return []
    return list(_read_yaml(common).get("not_questions", []))


def load_file(path: Path, root: Path) -> Scenario:
    raw = _read_yaml(path)
    raw["not_questions"] = _common_not_questions(root) + list(raw.get("not_questions", []))

    declared = raw.get("ground_truth") or {}
    forbidden = {"incident_type", "dds", "required_facts"} & set(declared)
    if forbidden:
        raise ScenarioError(
            f"{path.name}: {', '.join(sorted(forbidden))} в ground_truth выводится кодом "
            "и руками не пишется — иначе факты и эталон разъедутся"
        )

    extends = raw.get("extends")
    if extends:
        base_path = root / extends
        if not base_path.exists():
            raise ScenarioError(f"{path.name}: чек-лист {extends} не найден")
        base = _read_yaml(base_path).get("checklist", [])
        raw["checklist"] = [
            item.model_dump(exclude_none=True)
            for item in _merge_checklist(base, raw.get("checklist", []))
        ]

    try:
        scenario = Scenario.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ScenarioError(f"{path.name}: {where} — {first['msg']}") from exc

    known = scenario.fact_ids()
    for item in scenario.checklist:
        if item.fact and item.fact not in known:
            raise ScenarioError(
                f"{path.name}: пункт {item.id} ссылается на факт {item.fact}, которого нет"
            )
        if not item.question:
            raise ScenarioError(f"{path.name}: у пункта {item.id} нет текста вопроса")

    for fact in scenario.facts:
        question_id = fact.reveal_on.question if fact.reveal_on else None
        if question_id and question_id not in {item.id for item in scenario.checklist}:
            raise ScenarioError(
                f"{path.name}: факт {fact.id} раскрывается вопросом {question_id}, "
                "которого нет в чек-листе"
            )

    return _derive_ground_truth(scenario)


def load_library(root: Path) -> list[Scenario]:
    """Все сценарии каталога. Подкаталог `checklists/` — не сценарии."""
    if not root.exists():
        raise ScenarioError(f"каталог сценариев не найден: {root}")

    scenarios: list[Scenario] = []
    seen: dict[str, Path] = {}
    for path in sorted(root.glob("*.yaml")):
        scenario = load_file(path, root)
        if scenario.id in seen:
            raise ScenarioError(f"{path.name}: id {scenario.id} уже занят {seen[scenario.id].name}")
        seen[scenario.id] = path
        scenarios.append(scenario)

    if not scenarios:
        raise ScenarioError(f"в {root} нет ни одного сценария")
    return scenarios
