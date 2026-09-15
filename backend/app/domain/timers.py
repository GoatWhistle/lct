"""Таймеры ГОСТ Р 22.7.03-2021: коды, нормативы, состояние.

Норматив: docs/spec/NORMATIVES.md#временные-нормативы-приёма-вызова
Правило: таймеры останавливаются событиями, а не таймаутами
(docs/arch/CONTRACT.md) — иначе метрика недоказуема.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

GOST_REF = "ГОСТ Р 22.7.03-2021"


class TimerCode(StrEnum):
    """Коды одинаковы на бэке, фронте и в отчёте."""

    ANSWER = "answer"
    INTERVIEW = "interview"
    DDS_NOTIFY = "dds_notify"
    DDS_ACK = "dds_ack"
    ZONE_CHECK = "zone_check"
    CALLBACK = "callback"
    CLOSE = "close"


class TimerState(StrEnum):
    """Единственное место, где цвет несёт смысл (docs/arch/FRONTEND.md)."""

    OK = "ok"
    WARN = "warn"
    VIOLATED = "violated"


class Normative(BaseModel):
    """Норматив одной операции приёма вызова."""

    code: TimerCode
    title: str
    limit_ms: int
    attempts: int = 1
    ref: str = GOST_REF


#: Значения по умолчанию. Рабочие лимиты берутся из конфига (app.config),
#: не из кода — норматив может отличаться у заказчика.
NORMATIVES: dict[TimerCode, Normative] = {
    TimerCode.ANSWER: Normative(
        code=TimerCode.ANSWER, title="Ответ на звонок", limit_ms=8_000
    ),
    TimerCode.INTERVIEW: Normative(
        code=TimerCode.INTERVIEW, title="Опрос / идентификация ситуации", limit_ms=75_000
    ),
    TimerCode.DDS_NOTIFY: Normative(
        code=TimerCode.DDS_NOTIFY, title="Оповещение ДДС", limit_ms=60_000
    ),
    TimerCode.DDS_ACK: Normative(
        code=TimerCode.DDS_ACK, title="Подтверждение получения карточки", limit_ms=4_000
    ),
    TimerCode.ZONE_CHECK: Normative(
        code=TimerCode.ZONE_CHECK, title="Проверка зоны ответственности", limit_ms=30_000
    ),
    TimerCode.CALLBACK: Normative(
        code=TimerCode.CALLBACK,
        title="Обратный дозвон при обрыве",
        limit_ms=10_000,
        attempts=3,
    ),
    TimerCode.CLOSE: Normative(
        code=TimerCode.CLOSE, title="Снятие с контроля", limit_ms=300_000
    ),
}

#: Доля норматива, после которой таймер жёлтый.
WARN_RATIO = 0.8


def state_for(elapsed_ms: int, limit_ms: int) -> TimerState:
    """Состояние таймера по факту и нормативу."""
    if elapsed_ms > limit_ms:
        return TimerState.VIOLATED
    if elapsed_ms >= limit_ms * WARN_RATIO:
        return TimerState.WARN
    return TimerState.OK


class TimerSnapshot(BaseModel):
    """Один таймер в событии `timer.tick`."""

    code: TimerCode
    elapsed_ms: int
    limit_ms: int
    state: TimerState
    attempt: int = Field(default=1, description="Номер попытки для callback")
    stopped: bool = Field(default=False, description="Остановлен событием, не тикает")
