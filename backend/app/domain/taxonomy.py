"""Таксономия ошибок E1–E6 и компетенции.

Методика: docs/product/METHODOLOGY.md#таксономия-ошибок
Правило: каждая отметка несёт код и обоснование. Отметок без объяснения
не появляется ни в одном интерфейсе.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ErrorCode(StrEnum):
    """E — ошибки приёма вызова, D — ошибки диспетчера ДДС.

    Две роли, две таксономии. D1–D6 взяты из памятки заказчика дословно:
    она перечисляет типовые нарушения с реальными примерами, и преподаватель
    узнаёт их формулировки (docs/spec/DATASET.md).
    """

    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"
    E5 = "E5"
    E6 = "E6"
    D1 = "D1"
    D2 = "D2"
    D3 = "D3"
    D4 = "D4"
    D5 = "D5"
    D6 = "D6"


class FindingSource(StrEnum):
    """Источник объяснимой отметки; `judge` оставлен для старых отчётов."""

    SLOTS = "slots"
    DISPATCHER = "dispatcher"
    GROUND_TRUTH = "ground_truth"
    TIMERS = "timers"
    KIO = "kio"
    CHAIN = "chain"
    GRAMMAR = "grammar"
    # Legacy value: historical saved reports may still contain it.
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
        title="Грамматическая ошибка",
        detail="Нарушен включённый критерий грамматики описания КИО",
        source=FindingSource.GRAMMAR,
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
    ErrorCode.D1: ErrorSpec(
        code=ErrorCode.D1,
        title="Статус реагирования не проставлен",
        detail="Карточка осталась без первичного статуса и ушла в «Не оповещено»",
        source=FindingSource.DISPATCHER,
    ),
    ErrorCode.D2: ErrorSpec(
        code=ErrorCode.D2,
        title="Статус не соответствует факту",
        detail="«Принята» там, где работы проводиться не будут, и наоборот",
        source=FindingSource.DISPATCHER,
    ),
    ErrorCode.D3: ErrorSpec(
        code=ErrorCode.D3,
        title="Отказ от профильного происшествия",
        detail="Служба есть в списке оповещения, значит происшествие её — отказ неправомерен",
        source=FindingSource.DISPATCHER,
    ),
    ErrorCode.D4: ErrorSpec(
        code=ErrorCode.D4,
        title="Нет комментария к отказу",
        detail="«Не принята» и «Отказ от выполнения работ» без причины",
        source=FindingSource.DISPATCHER,
    ),
    ErrorCode.D5: ErrorSpec(
        code=ErrorCode.D5,
        title="Неполный комментарий",
        detail="В комментарии не назван получатель переданных сведений",
        source=FindingSource.DISPATCHER,
    ),
    ErrorCode.D6: ErrorSpec(
        code=ErrorCode.D6,
        title="Нет статусов хода работ",
        detail="Реагирование шло, но начало, прибытие и завершение не отмечены",
        source=FindingSource.DISPATCHER,
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
