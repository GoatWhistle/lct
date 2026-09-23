"""Локальное, детерминированное редактирование учебных сценариев.

Это копирование утверждённого шаблона, а не генерация нового сюжета ИИ.
Никаких сетевых вызовов в этом модуле нет.
"""

from copy import deepcopy
import re

from pydantic import ValidationError

from app.scenarios.loader import ScenarioError, _derive_ground_truth
from app.scenarios.schema import Scenario

DERIVED_TRUTH = {"incident_type", "dds", "incident_code", "notify", "required_facts"}
PLACEHOLDER = re.compile(r"\{\{?\s*[a-zA-Z_][\w.-]*\s*\}?\}")


def classifier_sign_gaps(signs: list[str], narrative: list[str]) -> list[str]:
    """Найти признаки ЕКП, которые не подтверждены текстом сценария.

    Сравниваются трёхбуквенные основы, поэтому русские окончания
    (``жилой дом`` / ``жилого дома``) не создают ложного расхождения. Это
    намеренно строгая, детерминированная граница: модель не может заявить
    один объект, а в карточку незаметно положить код другого.
    """
    def stems(value: str) -> set[str]:
        return {
            word[:3]
            for word in re.findall(r"[0-9a-zа-яё]+", value.casefold())
            if len(word) >= 3
        }

    present = stems(" ".join(narrative))
    gaps: list[str] = []
    for sign in signs:
        required = stems(sign)
        if required and not required <= present:
            gaps.append(sign)
    return gaps


def template_copy(source: Scenario, draft_id: str, title: str | None = None) -> dict:
    """Сделать полностью самостоятельный шаблонный черновик из сценария."""
    body = source.model_dump(mode="json")
    body["id"] = draft_id
    body["title"] = title or f"{source.title} — копия"
    body["extends"] = None  # чек-лист уже развёрнут загрузчиком
    body["ticket"] = None
    body["position"] = None
    for key in DERIVED_TRUTH:
        body["ground_truth"].pop(key, None)
    return body


def merge_patch(body: dict, patch: dict) -> dict:
    """JSON Merge Patch: словари сливаются, списки заменяются целиком.

    `id` — идентичность записи, а вычисляемые поля эталона не принимаются от
    клиента. Некорректные обычные поля можно временно сохранить в черновике;
    утверждение всё равно потребует полной проверки.
    """
    if "id" in patch and patch["id"] != body["id"]:
        raise ScenarioError("id сценария менять нельзя")
    truth = patch.get("ground_truth")
    if isinstance(truth, dict) and DERIVED_TRUTH & truth.keys():
        raise ScenarioError("вычисляемые поля ground_truth нельзя задавать вручную")

    result = deepcopy(body)

    def apply(target: dict, changes: dict) -> None:
        for key, value in changes.items():
            if value is None:
                target.pop(key, None)
            elif isinstance(value, dict) and isinstance(target.get(key), dict):
                apply(target[key], value)
            else:
                target[key] = deepcopy(value)

    apply(result, patch)
    result["id"] = body["id"]
    return result


def validate(body: dict) -> Scenario:
    """Проверить схему и связи фактов, затем пересчитать эталон по ЕКП."""
    declared = body.get("ground_truth") or {}
    if not isinstance(declared, dict):
        raise ScenarioError("ground_truth: ожидается объект")
    forbidden = DERIVED_TRUTH & declared.keys()
    if forbidden:
        raise ScenarioError(
            f"{', '.join(sorted(forbidden))} в ground_truth выводится кодом и руками не пишется"
        )
    try:
        scenario = Scenario.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise ScenarioError(f"{where}: {first['msg']}") from exc

    if not scenario.title.strip() or len(scenario.title) > 200:
        raise ScenarioError("title: требуется название длиной от 1 до 200 символов")
    if not scenario.first_line.strip():
        raise ScenarioError("first_line: требуется первая реплика")
    if not scenario.facts or not scenario.checklist:
        raise ScenarioError("сценарию нужны факты и чек-лист для оценки занятия")

    known = scenario.fact_ids()
    question_ids = {item.id for item in scenario.checklist}
    if len(known) != len(scenario.facts):
        raise ScenarioError("id фактов должны быть уникальны")
    if len(question_ids) != len(scenario.checklist):
        raise ScenarioError("id пунктов чек-листа должны быть уникальны")
    for item in scenario.checklist:
        if not item.question:
            raise ScenarioError(f"у пункта {item.id} нет текста вопроса")
        if item.fact and item.fact not in known:
            raise ScenarioError(f"пункт {item.id} ссылается на факт {item.fact}, которого нет")
    for fact in scenario.facts:
        if PLACEHOLDER.search(fact.value) or (fact.refined and PLACEHOLDER.search(fact.refined)):
            raise ScenarioError(
                f"факт {fact.id}: остался плейсхолдер вместо конкретного ответа заявителя"
            )
        question_id = fact.reveal_on.question if fact.reveal_on else None
        if question_id and question_id not in question_ids:
            raise ScenarioError(f"факт {fact.id} раскрывается несуществующим вопросом {question_id}")
        if fact.refine_on and fact.refine_on not in question_ids:
            raise ScenarioError(f"факт {fact.id} уточняется несуществующим вопросом {fact.refine_on}")
    # Для старых карточек и безопасных вариаций классификатор уже утверждён
    # заказчиком. Новый свободный сюжет обязан доказать свой путь ЕКП текстом.
    if scenario.id.startswith("ai-full-"):
        gaps = classifier_sign_gaps(
            scenario.signs,
            [scenario.title, scenario.first_line]
            + [fact.value for fact in scenario.facts]
            + [fact.refined for fact in scenario.facts if fact.refined],
        )
        if gaps:
            raise ScenarioError(
                "signs: сюжет не подтверждает путь ЕКП: " + ", ".join(gaps)
            )
    return _derive_ground_truth(scenario)
