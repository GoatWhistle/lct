"""Таймеры сессии.

**Правило: таймеры останавливаются событиями, а не таймаутами.** Иначе метрика
времени опроса становится недоказуемой, а вся оценка держится на том, что её
можно предъявить и проверить (docs/arch/CONTRACT.md).

Связь «событие → таймер» объявлена таблицей, а не разбросана по обработчикам:
так видно целиком, что чем запускается, и норматив нельзя потерять по дороге.
"""

import time
from dataclasses import dataclass, field

from app.domain.timers import NORMATIVES, TimerCode, TimerSnapshot, state_for

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
    "card.received": (TimerCode.ZONE_CHECK,),
    "call.dropped": (TimerCode.CALLBACK,),
    "callback.dial": (TimerCode.CALLBACK,),
}

#: Какое событие какой таймер останавливает.
STOPS: dict[str, tuple[TimerCode, ...]] = {
    "call.answer": (TimerCode.ANSWER,),
    "dds.dispatch": (TimerCode.INTERVIEW,),
    "card.ack": (TimerCode.DDS_ACK,),
    "zone.decision": (TimerCode.ZONE_CHECK,),
    "crew.arrived": (TimerCode.CLOSE,),
    "call.started": (TimerCode.CALLBACK,),
}


@dataclass
class Timer:
    code: TimerCode
    started_at: float | None = None
    elapsed_ms: int = 0
    attempt: int = 1
    stopped: bool = False

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


@dataclass
class SessionTimers:
    """Набор таймеров одной сессии. `limits` приходит из конфига —
    норматив меняется значением, а не правкой кода."""

    limits: dict[TimerCode, int] = field(
        default_factory=lambda: {code: norm.limit_ms for code, norm in NORMATIVES.items()}
    )
    timers: dict[TimerCode, Timer] = field(default_factory=dict)

    def on_event(self, event_type: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for code in STARTS.get(event_type, ()):
            self.timers.setdefault(code, Timer(code)).start(now)
        for code in STOPS.get(event_type, ()):
            self.timers.setdefault(code, Timer(code)).stop(now)

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
