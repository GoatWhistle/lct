"""Таймеры сессии.

**Правило: таймеры останавливаются событиями, а не таймаутами.** Иначе метрика
времени опроса становится недоказуемой, а вся оценка держится на том, что её
можно предъявить и проверить (docs/arch/CONTRACT.md).

Связь «событие → таймер» объявлена таблицей, а не разбросана по обработчикам:
так видно целиком, что чем запускается, и норматив нельзя потерять по дороге.
"""

import time
from datetime import UTC, datetime
from typing import Any

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    ValidationInfo,
    model_serializer,
    model_validator,
)

from app.domain.timers import NORMATIVES, TimerCode, TimerSnapshot, state_for


def now_utc() -> datetime:
    """Часы серверные. Метрика, посчитанная по часам браузера, недоказуема."""
    return datetime.now(UTC)


#: Какое событие какой таймер запускает.
#:
#: `dds_notify` (≤ 60 с) не запускается ничем, и это сознательно. Стартуй он
#: на ответе, как опрос, оба таймера мерили бы один отрезок с разными лимитами:
#: курсант, опросивший за законные 70 секунд, получал бы E3 «ДДС не оповещена
#: за 60 с», а на экране краснел бы таймер посреди нормального разговора.
#: Норматив, судя по порядку операций, отсчитывается от конца опроса — а события
#: «опрос закончен» в контракте нет. Вопрос к людям: docs/arch/CONTRACT.md.
STARTS: dict[str, tuple[TimerCode, ...]] = {
    "call.incoming": (TimerCode.ANSWER,),
    "call.answer": (TimerCode.INTERVIEW,),
    "dds.dispatch": (TimerCode.DDS_ACK, TimerCode.CLOSE),
    "dds.open": (TimerCode.DDS_WORK,),
    "card.start": (TimerCode.CARD_FILL,),
    "card.received": (TimerCode.ZONE_CHECK,),
    "call.dropped": (TimerCode.CALLBACK,),
    "callback.dial": (TimerCode.CALLBACK,),
}

#: Какое событие какой таймер останавливает.
STOPS: dict[str, tuple[TimerCode, ...]] = {
    "call.answer": (TimerCode.ANSWER,),
    "dds.dispatch": (TimerCode.INTERVIEW,),
    # Справка и передача в другой регион заканчивают опрос так же, как передача
    # карточки: норматив опроса не должен тикать после решения (lct-36).
    "call.resolve": (TimerCode.INTERVIEW,),
    "card.ack": (TimerCode.DDS_ACK,),
    "card.submit": (TimerCode.CARD_FILL,),
    "card.end": (TimerCode.CARD_FILL,),
    "dds.complete": (TimerCode.DDS_WORK,),
    "dds.finish": (TimerCode.DDS_WORK,),
    "zone.decision": (TimerCode.ZONE_CHECK,),
    "crew.arrived": (TimerCode.CLOSE,),
    "call.started": (TimerCode.CALLBACK,),
}


def downtime_ms(saved_at: datetime) -> int:
    """Сколько занятие пролежало в снимке: запущенный таймер считает и это время."""
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=UTC)
    return max(0, int((now_utc() - saved_at).total_seconds() * 1000))


class Timer(BaseModel):
    """`started_at` — monotonic-отметка процесса, в другом процессе она ничего
    не значит. Снимок хранит прошедшее время, а загрузка пересчитывает отметку
    от своих часов (простой берётся из контекста `downtime_ms`)."""

    code: TimerCode
    started_at: float | None = None
    elapsed_ms: int = 0
    attempt: int = 1
    stopped: bool = False

    @model_serializer(mode="wrap")
    def _dump(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = handler(self)
        data.pop("started_at")
        data["elapsed_ms"] = self.current_ms(time.monotonic())
        data["started"] = self.started_at is not None
        return data

    @model_validator(mode="before")
    @classmethod
    def _restore(cls, data: Any, info: ValidationInfo) -> Any:
        if not isinstance(data, dict) or "started" not in data:
            return data
        data = dict(data)
        elapsed = max(0, int(data.get("elapsed_ms", 0)))
        stopped = bool(data.get("stopped"))
        downtime = 0 if stopped else (info.context or {}).get("downtime_ms", 0)
        started = data.pop("started")
        data["started_at"] = time.monotonic() - (elapsed + downtime) / 1000 if started else None
        data["elapsed_ms"] = elapsed if stopped else 0
        return data

    def start(self, now: float) -> None:
        if self.stopped:
            # Повторный запуск после остановки — это новая попытка (обратный дозвон).
            self.attempt += 1
            self.stopped = False
            self.elapsed_ms = 0
        if self.started_at is None:
            self.started_at = now

    def stop(self, now: float) -> None:
        if self.started_at is not None and not self.stopped:
            self.elapsed_ms = int((now - self.started_at) * 1000)
        self.stopped = True

    def current_ms(self, now: float) -> int:
        if self.stopped or self.started_at is None:
            return self.elapsed_ms
        return int((now - self.started_at) * 1000)


class SessionTimers(BaseModel):
    """Набор таймеров одной сессии. `limits` приходит из конфига —
    норматив меняется значением, а не правкой кода."""

    limits: dict[TimerCode, int] = Field(
        default_factory=lambda: {code: norm.limit_ms for code, norm in NORMATIVES.items()}
    )
    timers: dict[TimerCode, Timer] = Field(default_factory=dict)

    def on_event(self, event_type: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for code in STARTS.get(event_type, ()):
            self.timers.setdefault(code, Timer(code=code)).start(now)
        for code in STOPS.get(event_type, ()):
            self.timers.setdefault(code, Timer(code=code)).stop(now)

    def snapshot(self, now: float | None = None) -> list[TimerSnapshot]:
        """Только запущенные таймеры: показывать нули по нормативам,
        до которых занятие ещё не дошло, значит пугать курсанта зря."""
        now = time.monotonic() if now is None else now
        result: list[TimerSnapshot] = []
        for code, timer in self.timers.items():
            limit = self.limits[code]
            elapsed = timer.current_ms(now)
            result.append(
                TimerSnapshot(
                    code=code,
                    elapsed_ms=elapsed,
                    limit_ms=limit,
                    state=state_for(elapsed, limit),
                    attempt=timer.attempt,
                    stopped=timer.stopped,
                )
            )
        return result

    def measured_ms(self, code: TimerCode) -> int | None:
        """Зафиксированное событием значение — то, что пойдёт в оценку."""
        timer = self.timers.get(code)
        if timer is None or not timer.stopped:
            return None
        return timer.elapsed_ms
