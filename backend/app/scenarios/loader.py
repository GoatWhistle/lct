"""Загрузчик библиотеки сценариев.

Проверяет **все** YAML на старте приложения и падает с внятным сообщением
при первом же нарушении: сломанный сценарий, найденный посреди занятия, —
сценарий, которого не должно случиться (docs/arch/BACKEND.md).
"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.domain import ekp
from app.domain.classifiers import DDS_BY_INCIDENT
from app.scenarios.schema import ChecklistItem, Scenario

# Библиотека читается на каждом старте приложения, а в тестах — на каждый
# TestClient: чистый Python-парсер тратит на неё ~1.7 с, libyaml — ~0.2 с.
_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class ScenarioError(Exception):
    """Ошибка библиотеки. Текст пишется для методиста, не для программиста."""


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)
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


def _assert_unique_checklist_ids(items: list[dict], source: str) -> None:
    ids = [
        item["id"] for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item_id in ids:
        if item_id in seen:
            duplicates.add(item_id)
        seen.add(item_id)
    if duplicates:
        raise ScenarioError(
            f"{source}: повторяются id пунктов чек-листа: {', '.join(sorted(duplicates))}"
        )


def _derive_ground_truth(scenario: Scenario) -> Scenario:
    """Эталон собирается кодом. Из YAML берутся только нормализованные
    адрес и число пострадавших — остальное перезаписывается."""
    required = [
        item.fact
        for item in scenario.checklist
        if item.fact
    ]
    scenario.ground_truth.incident_type = scenario.type
    scenario.ground_truth.dds = DDS_BY_INCIDENT[scenario.type]
    scenario.ground_truth.required_facts = required

    # Признаки → код ЕКП → список оповещения. Сценарий без признаков остаётся
    # рабочим: такие оцениваются по старым полям, пока их не разметят (lct-34).
    if scenario.signs:
        found = ekp.by_signs(scenario.signs)
        if found is None:
            raise ScenarioError(
                f"{scenario.id}: признаки {scenario.signs} не найдены в классификаторе ЕКП "
                f"версии {ekp.reference().version}"
            )
        scenario.ground_truth.incident_code = found.code
        # Эталонный список оповещения — с учётом обстоятельств вызова: пятеро
        # пострадавших поднимают скорую, и без них эталон был бы неполон.
        keys: list[str] = []
        if scenario.ground_truth.victims:
            keys.append("casualties")
        scenario.ground_truth.notify = ekp.notify_list(found.code, keys)
    return scenario


COMMON = "checklists/common.yaml"


def _common_not_questions(root: Path) -> list[str]:
    common = root / COMMON
    if not common.exists():
        return []
    return list(_read_yaml(common).get("not_questions", []))


def _common_checklist(root: Path, wanted: set[str]) -> list[dict]:
    """Общие пункты опроса — по требованию сценария, а не всем подряд.

    В `common.yaml` лежат формулировки пунктов, которые не принадлежат ни одному
    классификатору: проверка адреса, контакты заявителя. Сценарий подключает их,
    объявив пункт с тем же `id` и без текста, — текст и примеры подставятся сюда.
    Насильно добавлять их во все сценарии нельзя: непрочитанный пункт чек-листа
    штрафует курсанта отметкой E1 за вопрос, которого сценарий не требовал.
    """
    common = root / COMMON
    if not common.exists():
        return []
    return [
        item for item in _read_yaml(common).get("checklist", []) if item.get("id") in wanted
    ]


def load_file(path: Path, root: Path) -> Scenario:
    raw = _read_yaml(path)
    raw["not_questions"] = _common_not_questions(root) + list(raw.get("not_questions", []))

    declared = raw.get("ground_truth") or {}
    forbidden = {"incident_type", "dds", "required_facts", "incident_code", "notify"} & set(declared)
    if forbidden:
        raise ScenarioError(
            f"{path.name}: {', '.join(sorted(forbidden))} в ground_truth выводится кодом "
            "и руками не пишется — иначе факты и эталон разъедутся"
        )

    own = raw.get("checklist", [])
    _assert_unique_checklist_ids(own, path.name)
    base = _common_checklist(root, {item.get("id") for item in own})
    extends = raw.get("extends")
    if extends:
        base_path = root / extends
        if not base_path.exists():
            raise ScenarioError(f"{path.name}: чек-лист {extends} не найден")
        base = base + _read_yaml(base_path).get("checklist", [])
    _assert_unique_checklist_ids(base, f"{path.name}: подключённые чек-листы")
    if base:
        raw["checklist"] = [
            item.model_dump(exclude_none=True) for item in _merge_checklist(base, own)
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

    item_ids = {item.id for item in scenario.checklist}
    for fact in scenario.facts:
        question_id = fact.reveal_on.question if fact.reveal_on else None
        if question_id and question_id not in item_ids:
            raise ScenarioError(
                f"{path.name}: факт {fact.id} раскрывается вопросом {question_id}, "
                "которого нет в чек-листе"
            )
        if fact.refine_on and fact.refine_on not in item_ids:
            raise ScenarioError(
                f"{path.name}: факт {fact.id} уточняется вопросом {fact.refine_on}, "
                "которого нет в чек-листе"
            )

    scenario = _derive_ground_truth(scenario)
    _check_adjacent_services(scenario, path)
    return scenario


def _check_adjacent_services(scenario: Scenario, path: Path) -> None:
    """Смежные службы существуют только в списке оповещения карточки.

    Проверяется после вывода эталона: список оповещения считается из признаков
    ЕКП, и до `_derive_ground_truth` его ещё нет. Служба вне списка означала бы
    отметки, которых диспетчер не может увидеть в карточке.
    """
    recipients = set(scenario.notify_services())
    for entry in scenario.adjacent_timeline:
        if entry.service not in recipients:
            raise ScenarioError(
                f"{path.name}: adjacent_timeline: служба «{entry.service}» "
                "не входит в список оповещения карточки"
            )
    request = scenario.crew_request
    if request is not None and request.service not in recipients:
        raise ScenarioError(
            f"{path.name}: crew_request: служба «{request.service}» "
            "не входит в список оповещения карточки"
        )


#: Подкаталоги, которые сценариями не являются.
NOT_SCENARIOS = {"checklists", "pregenerated"}


def scenario_files(root: Path) -> list[Path]:
    """Файлы сценариев, включая подкаталоги вроде `tickets/`.

    Библиотека перестала быть плоской, когда в неё поехали билеты заказчика:
    тридцать два билета по три вызова — это девяносто шесть файлов, и держать
    их в одном каталоге рядом с чек-листами нельзя (docs/spec/TICKETS.md).
    """
    return sorted(
        path
        for path in root.rglob("*.yaml")
        if not set(path.relative_to(root).parts[:-1]) & NOT_SCENARIOS
    )


def load_library(root: Path) -> list[Scenario]:
    """Все сценарии каталога и его подкаталогов."""
    if not root.exists():
        raise ScenarioError(f"каталог сценариев не найден: {root}")

    scenarios: list[Scenario] = []
    seen: dict[str, Path] = {}
    for path in scenario_files(root):
        scenario = load_file(path, root)
        if scenario.id in seen:
            raise ScenarioError(f"{path.name}: id {scenario.id} уже занят {seen[scenario.id].name}")
        seen[scenario.id] = path
        scenarios.append(scenario)

    if not scenarios:
        raise ScenarioError(f"в {root} нет ни одного сценария")
    return scenarios
