"""Справочник ЕКП: признаки → итоговый тип → список оповещения.

Оператор системы-112 службу не выбирает. Он проставляет формализованные признаки
происшествия, комбинация признаков даёт итоговый тип, а итоговому типу соответствует
набор служб, который система формирует сама (docs/spec/DATASET.md).

Данные — `ekp.json`, собранный из книги заказчика скриптом `scripts/import_ekp.py`.
Грузится один раз и лениво: `make types` импортирует домен, и читать полмегабайта
ради генерации TypeScript незачем.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

DATA = Path(__file__).parent / "ekp.json"

#: Часть строк книги существует ради интеграции с чужими системами, а оператору
#: не показывается — книга помечает их прямо в первом признаке.
HIDDEN_SIGN = "не отображается оператору 112"

#: Условия, меняющие состав списка оповещения. Ключи — из `import_ekp.py`,
#: значения — то, как это называет оператор.
MODIFIER_LABELS: dict[str, str] = {
    "no_access": "нет доступа",
    "life_threat": "угроза людям",
    "casualties": "есть пострадавшие или погибшие",
    "gas": "газифицированный объект",
    "evacuation": "требуется эвакуация",
    "medical": "нужна медицинская помощь",
    "offence": "признаки правонарушения",
    "road_block": "перекрыто движение",
    "construction": "стройка",
    "tunnel": "тоннель",
    "pedestrian": "пешеходный объект",
    "vehicle": "автомобильный объект",
    "mass": "более пяти человек",
    "comm_object": "объект связи",
    "listed_object": "объект из перечня",
}


class Service(BaseModel):
    """Служба — участник информационного взаимодействия."""

    id: str
    name: str


class IncidentGroup(BaseModel):
    """Группа происшествий верхнего уровня: их 23, и это то, что преподаватель
    выбирает как тему занятия («категория событий» в ТЗ)."""

    id: int
    title: str


class Incident(BaseModel):
    """Одна строка классификатора: комбинация признаков и её последствия."""

    code: str = Field(description="Семизначный код ЕКП")
    group: int
    signs: list[str] = Field(description="Признаки 1–3, как их выбирает оператор")
    extra: str | None = Field(default=None, description="Дополнительные признаки")
    type: str = Field(description="Итоговый тип происшествия")
    ekp: str = Field(description="Тип по ЕКП-35")
    main: str | None = Field(default=None, description="Главная служба")
    notify: list[int] = Field(description="Индексы служб базового оповещения")
    notify_if: dict[str, list[int]] = Field(
        default_factory=dict, description="Службы, добавляемые модификатором"
    )


class Reference(BaseModel):
    """Справочник целиком. Версия — часть данных: классификатор обновляется,
    и в отчёте должно быть видно, по какой редакции считали."""

    version: str
    source: str
    imported_at: str
    groups: list[IncidentGroup]
    services: list[Service]
    incidents: list[Incident]


@lru_cache(maxsize=1)
def reference() -> Reference:
    return Reference.model_validate_json(DATA.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _by_code() -> dict[str, Incident]:
    return {incident.code: incident for incident in reference().incidents}


@lru_cache(maxsize=1)
def _by_signs() -> dict[tuple[str, ...], Incident]:
    """Признаки → строка классификатора. Нормализуем регистр: методист пишет
    «Жилой дом», книга — «жилой дом», и это одно и то же."""
    table: dict[tuple[str, ...], Incident] = {}
    for incident in reference().incidents:
        table.setdefault(tuple(sign.lower() for sign in incident.signs), incident)
    return table


def incident(code: str) -> Incident | None:
    return _by_code().get(code)


def by_signs(signs: list[str]) -> Incident | None:
    """Комбинация признаков → происшествие. Порядок признаков значим:
    в книге это три уровня, а не множество."""
    return _by_signs().get(tuple(sign.strip().lower() for sign in signs if sign.strip()))


def groups() -> list[IncidentGroup]:
    return reference().groups


def services() -> list[Service]:
    return reference().services


def service_names(indexes: list[int]) -> list[str]:
    known = reference().services
    return [known[i].name for i in indexes if 0 <= i < len(known)]


def notify_list(code: str, modifiers: list[str] | None = None) -> list[str]:
    """Список оповещения: базовый плюс то, что добавили модификаторы.

    Порядок — как в книге заказчика, и он не случаен: экстренные службы идут
    первыми, собирающие информацию — последними. Интерфейс показывает начало
    списка, поэтому сортировать его по-своему нельзя.
    """
    found = incident(code)
    if found is None:
        return []
    indexes = list(found.notify)
    for modifier in modifiers or []:
        for index in found.notify_if.get(modifier, []):
            if index not in indexes:
                indexes.append(index)
    indexes.sort()
    return service_names(indexes)


def signs_at_level(level: int, prefix: list[str] | None = None) -> list[str]:
    """Значения признака уровня 1–3 при уже выбранных предыдущих.

    Это источник выпадающих списков в карточке: выбрал «жилой дом» — во втором
    признаке остаются только те значения, что встречаются вместе с ним.
    """
    chosen = [sign.strip().lower() for sign in (prefix or [])]
    values: list[str] = []
    for item in reference().incidents:
        if len(item.signs) < level or item.signs[0].lower() == HIDDEN_SIGN:
            continue
        if [sign.lower() for sign in item.signs[: len(chosen)]] != chosen:
            continue
        value = item.signs[level - 1]
        if value not in values:
            values.append(value)
    return values
