"""Карточка информационного обмена (КИО).

Это не дизайн формы, это норматив: docs/spec/NORMATIVES.md#карточка-информационного-обмена-кио
Обязательность полей задаётся сценарием (`required_fields`), а не моделью:
в ложном вызове число пострадавших не требуется, в ЧС требуется.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.domain.classifiers import DDSCode, IncidentType


class Coords(BaseModel):
    """Координаты одним представлением на весь контракт.

    Именованные поля, а не кортеж: в `[55.75, 37.61]` невозможно увидеть,
    где широта, а где долгота, и ошибка всплывёт на карте, а не в типах.
    """

    lat: float
    lon: float


class ResponseStatus(StrEnum):
    REGISTERED = "registered"
    TRANSFERRED = "transferred"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"


class FireDetails(BaseModel):
    """Уточняющие поля 01 — МЧС."""

    fire_nature: str | None = None
    object_kind: str | None = None
    floors: int | None = None
    gasified: bool | None = None
    people_inside: bool | None = None
    smoke_spread: str | None = None  # куда идёт дым — пункт чек-листа 01


class PoliceDetails(BaseModel):
    """Уточняющие поля 02 — полиция."""

    offence_kind: str | None = None
    suspects: str | None = None
    suspect_fled: bool | None = None  # уехал ли нарушитель — пункт чек-листа 02
    vehicle: str | None = None


class MedicalDetails(BaseModel):
    """Уточняющие поля 03 — медицина."""

    reason: str | None = None
    conscious: bool | None = None
    breathing: bool | None = None
    can_move: bool | None = None
    age: int | None = None


class UtilityDetails(BaseModel):
    """Уточняющие поля 04 / ЖКХ."""

    failure_kind: str | None = None
    scale: str | None = None
    threat_to_residents: bool | None = None


class KIO(BaseModel):
    """Полная карточка. Наблюдателям уходит целиком (`kio.state`),
    курсанту — дельтой (`kio.patch`).

    Присваивание проверяется: без этого `card.dds = "03"` кладёт в карточку
    сырую строку вместо кода ДДС, и падает уже оценка, далеко от места ошибки.
    """

    model_config = ConfigDict(validate_assignment=True)

    # Служебное — заполняется системой
    card_id: UUID = Field(default_factory=uuid4)
    registered_at: datetime | None = None
    response_status: ResponseStatus = ResponseStatus.REGISTERED

    # Звонящий
    caller_number: str | None = None
    caller_name: str | None = None
    caller_contact: str | None = None
    language: str = "ru"

    # Место
    okato: str | None = None
    address: str | None = None
    street: str | None = None
    building: str | None = None
    entrance: str | None = None
    floor: str | None = None
    intercom_code: str | None = None  # спрашивается чек-листом 03, без него скорая стоит у двери
    coords: Coords | None = None

    # Происшествие. Оператор проставляет признаки, остальное считает система
    # по ЕКП (domain/ekp.py) — службу он не выбирает.
    incident_group: int | None = None
    signs: list[str] = Field(default_factory=list, max_length=3)
    incident_code: str | None = Field(default=None, description="Код ЕКП по признакам")
    incident_type: IncidentType | None = None
    description: str | None = None
    victims_count: int | None = None
    is_emergency: bool = False
    life_threat: bool = False
    evacuation_needed: bool = False

    # ДДС. `notify` считается из признаков и правится только системой:
    # в боевом АРМ оператор может добавить службу вручную, но не удалить.
    notify: list[str] = Field(default_factory=list, description="Список оповещения")
    dds: DDSCode | None = Field(
        default=None, description="Устаревшее: одна служба. Заменено списком оповещения"
    )
    dispatch_order_at: datetime | None = None
    arrival_at: datetime | None = None

    # Уточняющие по службе
    fire: FireDetails | None = None
    police: PoliceDetails | None = None
    medical: MedicalDetails | None = None
    utility: UtilityDetails | None = None


#: Поля, которые курсант не редактирует: их проставляет система.
READ_ONLY_FIELDS: frozenset[str] = frozenset(
    {"card_id", "registered_at", "caller_number", "response_status", "incident_code", "notify"}
)


def get_field(card: KIO, path: str) -> Any:
    """Значение поля по пути вида `floor` или `fire.floors`."""
    value: Any = card
    for part in path.split("."):
        if value is None:
            return None
        value = getattr(value, part, None)
    return value


def missing_fields(card: KIO, required: list[str]) -> list[str]:
    """Незаполненные обязательные поля — основание отметки E5.

    Обязательность приходит из сценария, пустой список означает,
    что сценарий требований к карточке не предъявляет.
    """
    empty: list[str] = []
    for path in required:
        value = get_field(card, path)
        if value is None or (isinstance(value, str) and not value.strip()):
            empty.append(path)
    return empty


#: Поля карточки, которые меняют состав списка оповещения. В боевом АРМ это
#: те же «признаки», только не про тип происшествия, а про обстоятельства:
#: пострадавшие поднимают скорую, газификация — МОСГАЗ (domain/ekp.py).
MODIFIER_FIELDS: frozenset[str] = frozenset(
    {"incident_group", "signs", "victims_count", "life_threat", "evacuation_needed", "fire.gasified"}
)


def modifiers(card: KIO) -> list[str]:
    """Какие условия оповещения включены обстоятельствами вызова."""
    keys: list[str] = []
    if card.victims_count:
        keys.append("casualties")
    if card.life_threat:
        keys.append("life_threat")
    if card.evacuation_needed:
        keys.append("evacuation")
    if card.fire is not None and card.fire.gasified:
        keys.append("gas")
    return keys


def derive_incident(card: KIO) -> KIO:
    """Пересчитать код происшествия и список оповещения по признакам.

    Вызывается после каждой правки признаков: в боевом АРМ список оповещения
    пересобирается сам, и это главное, чему учит карточка.
    """
    from app.domain import ekp

    found = ekp.by_signs(card.signs, card.incident_group)
    keys = modifiers(card)
    data = card.model_dump()
    data["incident_code"] = found.code if found else None
    # `notify` недоступен оператору для записи (READ_ONLY_FIELDS), поэтому
    # хранить прошлый авторасчёт как «добавленные вручную службы» нельзя:
    # при снятии признака газа МОСГАЗ должен исчезнуть из нового расчёта.
    data["notify"] = ekp.notify_list(found.code, keys) if found else []
    return KIO.model_validate(data)


def apply_patch(card: KIO, fields: dict[str, Any]) -> KIO:
    """Применить дельту `kio.patch`. Служебные поля игнорируются.

    Вложенные поля приходят плоским путём: {"fire.floors": 5}.
    """
    data = card.model_dump()
    for path, value in fields.items():
        if path in READ_ONLY_FIELDS:
            continue
        head, _, tail = path.partition(".")
        if not tail:
            data[head] = value
            continue
        nested = data.get(head) or {}
        nested[tail] = value
        data[head] = nested
    updated = KIO.model_validate(data)
    # Пересчитываем не только на смену признаков: пострадавшие поднимают скорую,
    # газификация — МОСГАЗ, и список оповещения обязан это отразить сразу.
    touched = MODIFIER_FIELDS & set(fields)
    return derive_incident(updated) if touched else updated
