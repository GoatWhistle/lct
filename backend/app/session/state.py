"""Состояние живой сессии: карточка, транскрипт, таймеры, режим, попытка.

Живёт в памяти процесса — поэтому воркер uvicorn ровно один: с двумя
преподаватель подключился бы к другому процессу, чем курсант, и увидел
пустой экран (docs/arch/STACK.md).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.domain.events import (
    CallEndReason,
    Mood,
    SessionMode,
    SessionSnapshot,
    Speaker,
    TranscriptEntry,
)
from app.dialog.persona import PersonaState
from app.dialog.slots import SlotMachine
from app.domain.kio import KIO, apply_patch
from app.session.timers import SessionTimers


def now_utc() -> datetime:
    """Часы серверные. Метрика, посчитанная по часам браузера, недоказуема."""
    return datetime.now(timezone.utc)


@dataclass
class SessionState:
    session_id: UUID
    scenario_id: str
    scenario_title: str
    level: str
    mode: SessionMode
    required_fields: list[str] = field(default_factory=list)
    trainee_name: str | None = None
    attempt: int = 1

    kio: KIO = field(default_factory=KIO)
    transcript: list[TranscriptEntry] = field(default_factory=list)
    timers: SessionTimers = field(default_factory=SessionTimers)
    hints_shown: list[str] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)

    # Звонящий. Автомата нет, если не скачана модель эмбеддингов:
    # занятие идёт, подсказки откатываются на порядок чек-листа.
    slots: SlotMachine | None = None
    persona: PersonaState | None = None

    # Аудио курсанта. До голосового контура (lct-06) кадры только считаются —
    # этого достаточно, чтобы доказать, что звук доходит от микрофона до сервера.
    audio_frames: int = 0
    bad_frames: int = 0

    started_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: CallEndReason | None = None
    dispatched_card: KIO | None = None

    def on_event(self, event_type: str) -> None:
        """Единственная точка, где событие двигает таймеры."""
        self.timers.on_event(event_type)

    def append(self, speaker: Speaker, text: str, mood: Mood | None = None) -> TranscriptEntry:
        entry = TranscriptEntry(
            ref=f"u{len(self.transcript) + 1}",
            speaker=speaker,
            text=text,
            at=now_utc(),
            mood=mood,
        )
        self.transcript.append(entry)
        return entry

    def patch_kio(self, fields: dict[str, Any]) -> KIO:
        self.kio = apply_patch(self.kio, fields)
        return self.kio

    def dispatch(self, service: str) -> KIO:
        """Карточка замораживается снимком: оператор не должен иметь
        возможности дописать задним числом поле, которое забыл."""
        self.kio = apply_patch(self.kio, {"dds": service, "response_status": "transferred"})
        self.dispatched_card = self.kio.model_copy(deep=True)
        return self.dispatched_card

    @property
    def ended(self) -> bool:
        return self.ended_at is not None

    def snapshot(self) -> SessionSnapshot:
        """Полное состояние. Монитор в классе включают посреди занятия —
        он обязан показать текущее, а не ждать следующего события."""
        return SessionSnapshot(
            session_id=self.session_id,
            scenario_id=self.scenario_id,
            scenario_title=self.scenario_title,
            level=self.level,
            mode=self.mode,
            trainee_name=self.trainee_name,
            started_at=self.started_at,
            kio=self.kio,
            required_fields=self.required_fields,
            transcript=list(self.transcript),
            timers=self.timers.snapshot(),
            hints_used=len(self.hints_shown),
            ended=self.ended,
        )
