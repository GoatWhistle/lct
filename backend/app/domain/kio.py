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

    # Происшествие
    incident_type: IncidentType | None = None
    description: str | None = None
    victims_count: int | None = None
    is_emergency: bool = False
    life_threat: bool = False
    evacuation_needed: bool = False

    # ДДС
    dds: DDSCode | None = None
    dispatch_order_at: datetime | None = None
    arrival_at: datetime | None = None

    # Уточняющие по службе
    fire: FireDetails | None = None
    police: PoliceDetails | None = None
    medical: MedicalDetails | None = None
    utility: UtilityDetails | None = None


#: Поля, которые курсант не редактирует: их проставляет система.
READ_ONLY_FIELDS: frozenset[str] = frozenset(
    {"card_id", "registered_at", "caller_number", "response_status"}
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
    return KIO.model_validate(data)
