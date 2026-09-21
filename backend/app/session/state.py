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
    Exercise,
    Mood,
    SessionMode,
    SessionSnapshot,
    Speaker,
    TranscriptEntry,
)
from app.dialog.caller import TemplateCaller
from app.dialog.persona import PersonaState
from app.dialog.slots import SlotMachine
from app.scenarios.schema import Scenario
from app.domain.statuses import (
    NEXT,
    ServiceStatus,
    StationSnapshot,
    StatusEntry,
    PhoneReportRecord,
    card_status,
    check,
    current,
)
from app.domain.timers import NORMATIVES, TimerCode
from app.domain.kio import KIO, ResponseStatus, apply_patch
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
    exercise: Exercise = Exercise.CALL
    required_fields: list[str] = field(default_factory=list)
    trainee_name: str | None = None
    #: Чьё это занятие. Проставляется при запуске, когда курсант известен
    #: по учётной записи: по нему разбор закрывается от чужих (lct-23).
    trainee_id: UUID | None = None
    attempt: int = 1

    kio: KIO = field(default_factory=KIO)
    transcript: list[TranscriptEntry] = field(default_factory=list)
    timers: SessionTimers = field(default_factory=SessionTimers)
    hints_shown: list[str] = field(default_factory=list)
    #: Когда именно подсказывали — в разборе видно, какой пункт и на какой минуте.
    hints_log: list[tuple[str, datetime]] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)

    # Звонящий. Автомата нет, если не скачана модель эмбеддингов:
    # занятие идёт, подсказки откатываются на порядок чек-листа.
    # Своя копия сценария на занятие: директивы преподавателя правят факты
    # и эталон, и правка в одной группе не должна протекать в остальные.
    scenario: Scenario | None = None
    slots: SlotMachine | None = None
    persona: PersonaState | None = None
    caller: TemplateCaller | None = None
    # Голосовой контур звонка. Нет — если голос выключен или моделей нет:
    # тогда кадры микрофона только считаются.
    voice: object | None = None

    # Аудио курсанта. До голосового контура (lct-06) кадры только считаются —
    # этого достаточно, чтобы доказать, что звук доходит от микрофона до сервера.
    audio_frames: int = 0
    bad_frames: int = 0

    # Самооценка сдана — до неё курсант не видит автооценку.
    self_assessed: bool = False
    self_assessment: dict | None = None
    score: dict | None = None

    started_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: CallEndReason | None = None
    dispatched_card: KIO | None = None
    dispatched_at: datetime | None = None
    #: Поля, из-за которых диспетчер вернул карточку, — основание E6.
    bounced_fields: list[str] = field(default_factory=list)
    dds_log: list[tuple[str, datetime, str | None]] = field(default_factory=list)
    #: Отметки служб по списку оповещения — предмет второго режима занятия.
    status_log: list[StatusEntry] = field(default_factory=list)
    crew_selected: str | None = None
    crew_assignments: dict[str, str] = field(default_factory=dict)
    phone_reports: list[PhoneReportRecord] = field(default_factory=list)
    #: Чем курсант закрыл вызов, если не карточкой (lct-36).
    resolved_outcome: str | None = None
    resolve_comment: str = ""

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

    def dispatch(self, service: str | None = None) -> KIO:
        """Карточка замораживается снимком: оператор не должен иметь
        возможности дописать задним числом поле, которое забыл."""
        if service is not None:
            self.kio = apply_patch(self.kio, {"dds": service})
        self.kio.response_status = ResponseStatus.TRANSFERRED
        self.dispatched_card = self.kio.model_copy(deep=True)
        self.dispatched_at = now_utc()
        return self.dispatched_card

    def notified_services(self) -> list[str]:
        """Список оповещения переданной карточки. До передачи — пусто:
        отмечаться диспетчеру пока не по чему."""
        card = self.dispatched_card or self.kio
        return list(card.notify)

    def crew_options(self) -> list[str]:
        return [f"{service} — бригада №{number}"
                for service in self.notified_services() for number in (1, 2)]

    def crew_service(self, crew: str) -> str | None:
        return next((service for service in self.notified_services()
                     if crew in (f"{service} — бригада №1", f"{service} — бригада №2")), None)

    def set_service_status(
        self, service: str, status: ServiceStatus, comment: str = "", author: str = ""
    ) -> StatusEntry:
        """Отметка службы. Проверка перехода — до записи: недопустимый статус
        не должен попасть в журнал даже отвергнутым."""
        check(self.status_log, service, status, comment)
        entry = StatusEntry(
            service=service, status=status, at=now_utc(), comment=comment, author=author
        )
        self.status_log.append(entry)
        return entry

    def station_snapshot(self) -> StationSnapshot:
        """Что видит диспетчер: службы, их статусы и доступные переходы."""
        services = self.notified_services()
        statuses = {service: current(self.status_log, service) for service in services}
        elapsed = self.timers.measured_ms(TimerCode.DDS_ACK)
        limit = NORMATIVES[TimerCode.DDS_ACK].limit_ms
        overdue = self.dispatched_at is not None and (
            elapsed is None
            and (now_utc() - self.dispatched_at).total_seconds() * 1000 > limit
        )
        return StationSnapshot(
            services=services,
            statuses=statuses,
            available={service: list(NEXT[status]) for service, status in statuses.items()},
            card=card_status(self.status_log, services, notify_deadline_passed=overdue),
            log=list(self.status_log),
            crew_options=self.crew_options(),
            crew_selected=self.crew_selected,
            phone_reports=list(self.phone_reports),
        )

    def card_received_event(self):
        """Снимок карточки для станции ДДС."""
        from app.domain.events import CardReceived

        return CardReceived(
            card=self.dispatched_card,
            from_operator=("учебный сценарий" if self.exercise is Exercise.DDS
                           else self.trainee_name or "оператор 112"),
            at=self.dispatched_at or now_utc(),
        )

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
            exercise=self.exercise,
            trainee_name=self.trainee_name,
            started_at=self.started_at,
            kio=self.kio,
            required_fields=self.required_fields,
            transcript=list(self.transcript),
            timers=self.timers.snapshot(),
            hints_used=len(self.hints_shown),
            ended=self.ended,
        )
