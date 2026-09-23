"""Состояние живой сессии: карточка, транскрипт, таймеры, режим, попытка.

Живёт в памяти процесса — поэтому воркер uvicorn ровно один: с двумя
преподаватель подключился бы к другому процессу, чем курсант, и увидел
пустой экран (docs/arch/STACK.md).
"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.dialog.caller import TemplateCaller
from app.dialog.persona import PersonaState
from app.dialog.slots import SlotMachine
from app.domain.events import (
    CallEndReason,
    Exercise,
    LessonCriteria,
    Metric,
    Mood,
    SessionMode,
    SessionSnapshot,
    Speaker,
    TranscriptEntry,
)
from app.domain.kio import KIO, ResponseStatus, apply_patch
from app.domain.statuses import (
    CardStatus,
    NEXT,
    DdsCardSummary,
    DdsQueueCard,
    PhoneCallPending,
    PhoneLineRecord,
    PhoneReportRecord,
    ServiceStatus,
    StationSnapshot,
    StatusEntry,
    card_status,
    check,
    current,
)
from app.domain.taxonomy import Finding
from app.domain.timers import TimerCode
from app.scenarios.schema import Scenario
from app.scoring.grammar import GrammarAssessment
from app.session.timers import SessionTimers


def now_utc() -> datetime:
    """Часы серверные. Метрика, посчитанная по часам браузера, недоказуема."""
    return datetime.now(UTC)


@dataclass
class DdsCardRecord:
    card_id: UUID
    scenario_id: str
    reply_text: str
    metrics: list[Metric]
    findings: list[Finding]
    actions: list[dict[str, Any]]
    duration_ms: int

    @property
    def score_auto(self) -> float:
        total = sum(metric.weight for metric in self.metrics)
        passed = sum(metric.weight for metric in self.metrics if metric.passed)
        return round(100 * passed / total, 1) if total else 0.0


@dataclass
class DdsLiveCard:
    """Изолированное живое состояние одной одновременно выданной карточки."""

    original_index: int
    scenario: Scenario
    kio: KIO
    dispatched_card: KIO
    dispatched_at: datetime
    timers: SessionTimers
    bounced_fields: list[str] = field(default_factory=list)
    dds_log: list[tuple[str, datetime, str | None]] = field(default_factory=list)
    status_log: list[StatusEntry] = field(default_factory=list)
    crew_selected: str | None = None
    crew_assignments: dict[str, str] = field(default_factory=dict)
    phone_reports: list[PhoneReportRecord] = field(default_factory=list)
    phone_lines: list[PhoneLineRecord] = field(default_factory=list)
    phone_pending: PhoneCallPending | None = None
    reply_text: str = ""
    reply_grammar: GrammarAssessment | None = None
    reply_log: list[tuple[datetime, str]] = field(default_factory=list)

    @property
    def card_id(self) -> UUID:
        return self.dispatched_card.card_id


@dataclass
class SessionState:
    session_id: UUID
    scenario_id: str
    scenario_title: str
    level: str
    mode: SessionMode
    #: Преподаватель, создавший занятие; чужой пульт не может им управлять.
    owner_login: str | None = None
    exercise: Exercise = Exercise.CALL
    #: После заполнения КИО занятие продолжится на АРМ ДДС, а не завершится.
    handoff_to_dds: bool = False
    required_fields: list[str] = field(default_factory=list)
    trainee_name: str | None = None
    #: Чьё это занятие. Проставляется при запуске, когда курсант известен
    #: по учётной записи: по нему разбор закрывается от чужих (lct-23).
    trainee_id: UUID | None = None
    #: Конкретная ДДС обучающегося. Она меняет статусы только своей службы;
    #: остальные адресаты карточки показываются информационно.
    dds_service: str | None = None
    attempt: int = 1
    criteria: LessonCriteria = field(default_factory=LessonCriteria)

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
    recorder: object | None = None
    recording_path: str | None = None

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
    phone_lines: list[PhoneLineRecord] = field(default_factory=list)
    phone_pending: PhoneCallPending | None = None
    dds_scenarios: list[Scenario] = field(default_factory=list)
    dds_live_cards: list[DdsLiveCard] = field(default_factory=list)
    dds_active_card_id: UUID | None = None
    dds_card_index: int = 0
    dds_completed: list[DdsCardRecord] = field(default_factory=list)
    dds_arrival_interval_seconds: int = 0
    dds_max_waiting: int = 3
    dds_next_scenario_index: int = 0
    dds_next_arrival_at: datetime | None = None
    reply_text: str = ""
    reply_grammar: GrammarAssessment | None = None
    reply_log: list[tuple[datetime, str]] = field(default_factory=list)
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

    def _managed_services_for(self, recipients: list[str]) -> list[str]:
        """Служба, от имени которой работает этот обучающийся.

        Старые занятия и demo без профиля безопасно закрепляются за первым
        адресатом. Обобщённая учебная служба «ДДС района» соответствует
        территориальному органу, а не всем получателям карточки сразу.
        """
        if not recipients:
            return []
        wanted = (self.dds_service or "").strip().casefold()
        if wanted:
            exact = next((item for item in recipients if item.casefold() == wanted), None)
            if exact:
                return [exact]
            contained = next(
                (item for item in recipients
                 if wanted in item.casefold() or item.casefold() in wanted),
                None,
            )
            if contained:
                return [contained]
            if "район" in wanted:
                district = next(
                    (item for item in recipients if "территориальн" in item.casefold()), None
                )
                if district:
                    return [district]
            return []
        return [recipients[0]]

    def managed_services(self) -> list[str]:
        return self._managed_services_for(self.notified_services())

    def capture_active_dds(self) -> None:
        """Сохранить изменяемые поля активной карточки перед переключением."""
        if not self.dispatched_card:
            return
        card = next(
            (item for item in self.dds_live_cards
             if item.card_id == self.dispatched_card.card_id),
            None,
        )
        if card is None:
            return
        card.scenario = self.scenario
        card.kio = self.kio
        card.dispatched_card = self.dispatched_card
        card.dispatched_at = self.dispatched_at or card.dispatched_at
        card.timers = self.timers
        card.bounced_fields = self.bounced_fields
        card.dds_log = self.dds_log
        card.status_log = self.status_log
        card.crew_selected = self.crew_selected
        card.crew_assignments = self.crew_assignments
        card.phone_reports = self.phone_reports
        card.phone_lines = self.phone_lines
        card.phone_pending = self.phone_pending
        card.reply_text = self.reply_text
        card.reply_grammar = self.reply_grammar
        card.reply_log = self.reply_log
        self.dds_active_card_id = card.card_id

    def activate_dds_card(self, card_id: UUID, *, capture: bool = True) -> bool:
        """Переключить рабочее место на карточку, не останавливая остальные."""
        if capture:
            self.capture_active_dds()
        card = next((item for item in self.dds_live_cards if item.card_id == card_id), None)
        if card is None:
            return False
        scenario = card.scenario
        self.scenario = scenario
        self.scenario_id = scenario.id
        self.scenario_title = scenario.title
        self.level = scenario.level.value
        self.required_fields = list(scenario.required_fields)
        self.kio = card.kio
        self.dispatched_card = card.dispatched_card
        self.dispatched_at = card.dispatched_at
        self.timers = card.timers
        self.bounced_fields = card.bounced_fields
        self.dds_log = card.dds_log
        self.status_log = card.status_log
        self.crew_selected = card.crew_selected
        self.crew_assignments = card.crew_assignments
        self.phone_reports = card.phone_reports
        self.phone_lines = card.phone_lines
        self.phone_pending = card.phone_pending
        self.reply_text = card.reply_text
        self.reply_grammar = card.reply_grammar
        self.reply_log = card.reply_log
        self.dds_card_index = card.original_index
        self.dds_active_card_id = card.card_id
        return True

    def crew_options(self) -> list[str]:
        return [f"{service} — бригада №{number}"
                for service in self.managed_services() for number in (1, 2)]

    def crew_service(self, crew: str) -> str | None:
        return next((service for service in self.managed_services()
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
        self.capture_active_dds()
        has_active_dds_card = any(
            item.card_id == self.dds_active_card_id for item in self.dds_live_cards
        )
        recipients = self.notified_services() if has_active_dds_card or not self.dds_scenarios else []
        services = self._managed_services_for(recipients)
        statuses = {service: current(self.status_log, service) for service in services}
        elapsed = self.timers.measured_ms(TimerCode.DDS_ACK)
        limit = self.timers.limits[TimerCode.DDS_ACK]
        overdue = has_active_dds_card and self.dispatched_at is not None and (
            elapsed is None
            and (now_utc() - self.dispatched_at).total_seconds() * 1000 > limit
        )
        now_mono = time.monotonic()
        queue_cards: list[DdsQueueCard] = []
        for item in sorted(self.dds_live_cards, key=lambda queued: queued.original_index):
            recipients_for_card = list(item.dispatched_card.notify)
            managed = self._managed_services_for(recipients_for_card)
            service = managed[0] if managed else None
            timer = item.timers.timers.get(TimerCode.DDS_ACK)
            elapsed_ms = timer.current_ms(now_mono) if timer else 0
            stopped = timer.stopped if timer else False
            limit_ms = item.timers.limits[TimerCode.DDS_ACK]
            queue_cards.append(DdsQueueCard(
                card_id=item.card_id,
                scenario_id=item.scenario.id,
                title=item.scenario.title,
                address=item.dispatched_card.address,
                description=item.dispatched_card.description,
                incident_type=(item.dispatched_card.incident_type.value
                               if item.dispatched_card.incident_type else None),
                victims_count=item.dispatched_card.victims_count,
                received_at=item.dispatched_at,
                managed_service=service,
                service_status=(current(item.status_log, service)
                                if service else ServiceStatus.ADDED),
                card_status=card_status(
                    item.status_log, managed,
                    notify_deadline_passed=not stopped and elapsed_ms > limit_ms,
                ),
                elapsed_ms=elapsed_ms,
                limit_ms=limit_ms,
                timer_stopped=stopped,
                active=item.card_id == self.dds_active_card_id,
            ))
        return StationSnapshot(
            services=services,
            recipient_services=recipients,
            managed_service=services[0] if services else None,
            statuses=statuses,
            available={service: list(NEXT[status]) for service, status in statuses.items()},
            card=(card_status(self.status_log, services, notify_deadline_passed=overdue)
                  if has_active_dds_card or not self.dds_scenarios else CardStatus.REGISTERED),
            log=list(self.status_log),
            crew_options=(self.crew_options() if has_active_dds_card or not self.dds_scenarios else []),
            crew_selected=self.crew_selected if has_active_dds_card else None,
            phone_reports=list(self.phone_reports) if has_active_dds_card else [],
            phone_lines=list(self.phone_lines) if has_active_dds_card else [],
            phone_pending=self.phone_pending if has_active_dds_card else None,
            card_id=(self.dispatched_card.card_id if self.dispatched_card and has_active_dds_card else None),
            card_index=(self.dds_card_index + 1 if has_active_dds_card
                        else min(self.dds_next_scenario_index + 1, len(self.dds_scenarios))),
            card_total=len(self.dds_scenarios) or 1,
            reply_text=self.reply_text if has_active_dds_card else "",
            completed_cards=[DdsCardSummary(card_id=item.card_id,
                                            scenario_id=item.scenario_id,
                                            score_auto=item.score_auto)
                             for item in self.dds_completed],
            queue_cards=queue_cards,
            pending_cards_count=max(
                0, len(self.dds_scenarios) - len(self.dds_completed)
                - (1 if has_active_dds_card else 0),
            ),
            next_arrival_in_seconds=(
                max(0, int((self.dds_next_arrival_at - now_utc()).total_seconds() + 0.999))
                if self.dds_next_arrival_at is not None else None
            ),
            max_waiting_cards=self.dds_max_waiting,
        )

    def card_received_event(self):
        """Снимок карточки для станции ДДС."""
        from app.domain.events import CardReceived

        return CardReceived(
            card=self.dispatched_card,
            from_operator=("учебный сценарий" if self.exercise is Exercise.DDS
                           else self.trainee_name or "оператор 112"),
            at=self.dispatched_at or now_utc(),
            card_index=self.dds_card_index + 1,
            card_total=len(self.dds_scenarios) or 1,
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
            criteria=self.criteria,
            trainee_name=self.trainee_name,
            started_at=self.started_at,
            kio=self.kio,
            required_fields=self.required_fields,
            transcript=list(self.transcript),
            timers=self.timers.snapshot(),
            hints_used=len(self.hints_shown),
            ended=self.ended,
        )
