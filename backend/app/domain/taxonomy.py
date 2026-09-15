"""Таксономия ошибок E1–E6 и компетенции.

Методика: docs/product/METHODOLOGY.md#таксономия-ошибок
Правило: каждая отметка несёт код и обоснование. Отметок без объяснения
не появляется ни в одном интерфейсе.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ErrorCode(StrEnum):
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"
    E5 = "E5"
    E6 = "E6"


class FindingSource(StrEnum):
    """Кто выставил отметку. `judge` — единственный недетерминированный источник."""

    SLOTS = "slots"
    GROUND_TRUTH = "ground_truth"
    TIMERS = "timers"
    KIO = "kio"
    CHAIN = "chain"
    JUDGE = "judge"
    INSTRUCTOR = "instructor"


class ErrorSpec(BaseModel):
    code: ErrorCode
    title: str
    detail: str
    source: FindingSource


ERRORS: dict[ErrorCode, ErrorSpec] = {
    ErrorCode.E1: ErrorSpec(
        code=ErrorCode.E1,
        title="Пропущенный факт",
        detail="Обязательный вопрос не прозвучал, факт не добыт",
        source=FindingSource.SLOTS,
    ),
    ErrorCode.E2: ErrorSpec(
        code=ErrorCode.E2,
        title="Неверная классификация или маршрутизация",
        detail="Тип происшествия или ДДС определены неверно",
        source=FindingSource.GROUND_TRUTH,
    ),
    ErrorCode.E3: ErrorSpec(
        code=ErrorCode.E3,
        title="Нарушение временного норматива",
        detail="Опрос дольше 75 с, ДДС не оповещена за 60 с и т. д.",
        source=FindingSource.TIMERS,
    ),
    ErrorCode.E4: ErrorSpec(
        code=ErrorCode.E4,
        title="Коммуникативная ошибка",
        detail="Тон, эмпатия, управление диалогом, лишние вопросы, игнорирование паники",
        source=FindingSource.JUDGE,
    ),
    ErrorCode.E5: ErrorSpec(
        code=ErrorCode.E5,
        title="Неполнота карточки",
        detail="Обязательные поля КИО пусты или заполнены неверно",
        source=FindingSource.KIO,
    ),
    ErrorCode.E6: ErrorSpec(
        code=ErrorCode.E6,
        title="Неверное завершение",
        detail="Карточка ушла в ДДС непригодной, снятие с контроля до подтверждения",
        source=FindingSource.CHAIN,
    ),
}


class Competency(StrEnum):
    """Шесть осей радара. Проекция уже посчитанных метрик, без пересчёта весов."""

    INTAKE = "intake"
    INTERVIEW = "interview"
    CARD = "card"
    ROUTING = "routing"
    NORMS = "norms"
    COMMUNICATION = "communication"


COMPETENCY_LABELS: dict[Competency, str] = {
    Competency.INTAKE: "Приём вызова",
    Competency.INTERVIEW: "Опрос и добыча фактов",
    Competency.CARD: "Заполнение КИО",
    Competency.ROUTING: "Классификация и маршрутизация",
    Competency.NORMS: "Соблюдение нормативов",
    Competency.COMMUNICATION: "Коммуникация в стрессе",
}


class Finding(BaseModel):
    """Отметка в разборе. `fact` и `norm` — то самое обоснование."""

    code: ErrorCode
    source: FindingSource
    summary: str
    fact: str
    norm: str | None = None
    ref: str | None = None
    competency: Competency | None = None
    transcript_ref: str | None = None
    at: datetime | None = None
