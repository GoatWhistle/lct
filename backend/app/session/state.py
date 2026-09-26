"""Состояние живой сессии: карточка, транскрипт, таймеры, режим, попытка.

Живёт в памяти процесса — поэтому воркер uvicorn ровно один: с двумя
преподаватель подключился бы к другому процессу, чем курсант, и увидел
пустой экран (docs/arch/STACK.md).
"""

import time
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.dialog.caller import TemplateCaller
from app.dialog.persona import PersonaState
from app.dialog.slots import SlotMachine
from app.domain.events import (
    CallEndReason,
    Exercise,
    LessonCriteria,
    Mood,
    SessionMode,
    SessionSnapshot,
    Speaker,
    TranscriptEntry,
)
from app.domain.kio import KIO, ResponseStatus, apply_patch
from app.domain.statuses import (
    NEXT,
    CardStatus,
    DdsCardSummary,
    DdsQueueCard,
    ServiceStatus,
    StationSnapshot,
    card_status,
    current,
)
from app.domain.timers import TimerCode
from app.scenarios.schema import Scenario
from app.session.dds import DdsCardRecord, DdsDesk, DdsLiveCard
from app.session.timers import SessionTimers, now_utc

__all__ = ["DdsCardRecord", "DdsLiveCard", "PersistedSession", "SessionState", "now_utc"]

#: Сколько последних команд станции помнит защита от повтора.
MAX_STATION_COMMANDS = 512


class PersistedSession(BaseModel):
    """Сохраняемая часть занятия — ровно то, что пишется в снимок.

    Снимок — `model_dump` этой модели, загрузка — `model_validate`, поэтому
    поле сессии нельзя завести, не решив, сохраняемое оно или живое: подкласс
    обязан пометить каждое своё поле `Field(exclude=True)`, иначе класс не
    создастся (см. `__pydantic_init_subclass__`).
    """

    session_id: UUID
    scenario_id: str
    scenario_title: str
    level: str
    mode: SessionMode
    #: Преподаватель, создавший занятие; чужой пульт не может им управлять.
    owner_login: str | None = None
    #: Monotonic DB ownership generation; stale processes may not persist writes.
    backend_fencing_epoch: int = 0
    exercise: Exercise = Exercise.CALL
    #: После заполнения КИО занятие продолжится на АРМ ДДС, а не завершится.
    handoff_to_dds: bool = False
    required_fields: list[str] = Field(default_factory=list)
    trainee_name: str | None = None
    #: Чьё это занятие. Проставляется при запуске, когда курсант известен
    #: по учётной записи: по нему разбор закрывается от чужих (lct-23).
    trainee_id: UUID | None = None
    #: Конкретная ДДС обучающегося. Она меняет статусы только своей службы;
    #: остальные адресаты карточки показываются информационно.
    dds_service: str | None = None
    attempt: int = 1
    criteria: LessonCriteria = Field(default_factory=LessonCriteria)

    kio: KIO = Field(default_factory=KIO)
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    timers: SessionTimers = Field(default_factory=SessionTimers)
    hints_shown: list[str] = Field(default_factory=list)
    #: Когда именно подсказывали — в разборе видно, какой пункт и на какой минуте.
    hints_log: list[tuple[str, datetime]] = Field(default_factory=list)
    notes: list[dict] = Field(default_factory=list)
    directives: list[str] = Field(default_factory=list)

    # Своя копия сценария на занятие: директивы преподавателя правят факты
    # и эталон, и правка в одной группе не должна протекать в остальные.
    scenario: Scenario | None = None

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
    #: Карточка оператора 112, замороженная при передаче в ДДС.
    dispatched_card: KIO | None = None
    dispatched_at: datetime | None = None
    #: Поля, из-за которых диспетчер вернул карточку, — основание E6.
    bounced_fields: list[str] = Field(default_factory=list)
    #: Готовые карточки связки 112→ДДС ждут, пока курсант не сдаст свою.
    pending_dds_scenarios: list[Scenario] = Field(default_factory=list)
    #: Исходная часть упражнения 112→ДДС сохраняется отдельно от карточек пульта.
    operator_kio: KIO | None = None
    operator_scenario: Scenario | None = None
    #: Работа диспетчера живёт только здесь — в карточках пульта.
    desk: DdsDesk = Field(default_factory=DdsDesk)
    #: Чем курсант закрыл вызов, если не карточкой (lct-36).
    resolved_outcome: str | None = None
    resolve_comment: str = ""
    #: Recently committed DDS command IDs; included in the durable checkpoint so
    #: a lost WebSocket acknowledgement cannot apply an operation twice.
    processed_station_commands: list[str] = Field(default_factory=list)
    text_revealed_facts: dict[str, str] = Field(default_factory=dict)

    @field_validator("processed_station_commands")
    @classmethod
    def _recent_commands(cls, value: list[str]) -> list[str]:
        return value[-MAX_STATION_COMMANDS:]

    @field_serializer("processed_station_commands")
    def _dump_recent_commands(self, value: list[str]) -> list[str]:
        return value[-MAX_STATION_COMMANDS:]

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        undecided = [name for name, info in cls.model_fields.items()
                     if name not in PersistedSession.model_fields and not info.exclude]
        if undecided:
            raise TypeError(
                f"{cls.__name__}: поля {undecided} — сохраняемое поле объявляется в "
                "PersistedSession, живое — с Field(exclude=True)"
            )


class SessionState(PersistedSession):
    """Живое занятие: сохраняемая часть плюс runtime-объекты процесса.

    Живые поля в снимок не попадают и пересобираются при загрузке
    (`checkpoint.rebuild_live`). Прогресс звонящего (`slots`, `persona`) пока
    тоже живой: после failover звонящий начинает с чистого листа — голос P2,
    а потеря редкая. Чтобы сохранять его, достаточно добавить в
    `PersistedSession` поле `CallerProgress` и передать его в `rebuild_live`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: Runtime-only: set when this process loses or cannot confirm DB ownership.
    lease_fenced: bool = Field(default=False, exclude=True)
    # Звонящий. Автомата нет, если не скачана модель эмбеддингов:
    # занятие идёт, подсказки откатываются на порядок чек-листа.
    slots: SlotMachine | None = Field(default=None, exclude=True)
    persona: PersonaState | None = Field(default=None, exclude=True)
    caller: TemplateCaller | None = Field(default=None, exclude=True)
    # Голосовой контур звонка. Нет — если голос выключен или моделей нет:
    # тогда кадры микрофона только считаются.
    voice: object | None = Field(default=None, exclude=True)
    recorder: object | None = Field(default=None, exclude=True)
    recording_path: str | None = Field(default=None, exclude=True)

    def persisted(self) -> PersistedSession:
        """Сохраняемая часть без копирования — то, что уходит в снимок."""
        return PersistedSession.model_construct(
            **{name: getattr(self, name) for name in PersistedSession.model_fields}
        )

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
        if self.exercise is Exercise.CALL:
            self._receive_call_card()
        return self.dispatched_card

    def _receive_call_card(self) -> None:
        """Живой диспетчер упражнения 112 работает с переданной карточкой на пульте.

        Таймеры общие с оператором: норматив приёма ДДС в этом упражнении
        часть одной цепочки 112 → ДДС и оценивается вместе с ней. Повторная
        передача обновляет содержимое карточки, не стирая работу диспетчера.
        """
        if self.scenario is None or self.dispatched_card is None:
            return
        card = self.desk.cards.get(self.dispatched_card.card_id)
        if card is None:
            card = self.desk.add(DdsLiveCard(
                original_index=0,
                scenario=self.scenario,
                kio=self.kio,
                dispatched_card=self.dispatched_card,
                dispatched_at=self.dispatched_at,
                timers=self.timers,
            ))
        else:
            card.kio = self.kio
            card.dispatched_card = self.dispatched_card
            card.dispatched_at = self.dispatched_at
        self.desk.open(card.card_id)

    @property
    def dds_phase(self) -> bool:
        """Экран курсанта — пульт с очередью: упражнение ДДС или вторая часть связки."""
        return self.exercise is Exercise.DDS or (self.handoff_to_dds and bool(self.desk.scenarios))

    def shown_card(self) -> DdsLiveCard | None:
        """Карточка, которую монитор показывает вместо КИО оператора."""
        return self.desk.active if self.dds_phase else None

    def shown_timers(self) -> SessionTimers:
        card = self.shown_card()
        return card.timers if card is not None else self.timers

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
        """Службы обучающегося: по активной карточке пульта, до неё — по КИО 112."""
        card = self.desk.active
        if card is not None:
            return self.card_services(card)
        return self._managed_services_for(self.notified_services())

    def card_services(self, card: DdsLiveCard) -> list[str]:
        """Службы карточки пульта, от имени которых работает обучающийся."""
        return self._managed_services_for(list(card.dispatched_card.notify))

    @staticmethod
    def _crew_options_for(services: list[str]) -> list[str]:
        return [f"{service} — бригада №{number}" for service in services for number in (1, 2)]

    def crew_options(self, card: DdsLiveCard) -> list[str]:
        return self._crew_options_for(self.card_services(card))

    def crew_service(self, card: DdsLiveCard, crew: str) -> str | None:
        return next((service for service in self.card_services(card)
                     if crew in (f"{service} — бригада №1", f"{service} — бригада №2")), None)

    def station_snapshot(self) -> StationSnapshot:
        """Что видит диспетчер: службы, их статусы и доступные переходы."""
        desk = self.desk
        card = desk.active
        if card is not None:
            recipients = list(card.dispatched_card.notify)
        elif not desk.scenarios:
            # Пульт ещё пуст (до передачи из 112) или упражнение без ДДС:
            # показываем список оповещения карточки оператора.
            recipients = self.notified_services()
        else:
            recipients = []
        services = self._managed_services_for(recipients)
        log = list(card.status_log) if card is not None else []
        statuses = {service: current(log, service) for service in services}
        overdue = False
        if card is not None:
            elapsed = card.timers.measured_ms(TimerCode.DDS_ACK)
            limit = card.timers.limits[TimerCode.DDS_ACK]
            overdue = elapsed is None and (
                (now_utc() - card.dispatched_at).total_seconds() * 1000 > limit
            )
        now_mono = time.monotonic()
        queue_cards: list[DdsQueueCard] = []
        for item in (desk.ordered() if self.dds_phase else []):
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
                active=item.card_id == desk.active_id,
            ))
        shows_card = card is not None or not desk.scenarios
        return StationSnapshot(
            services=services,
            recipient_services=recipients,
            managed_service=services[0] if services else None,
            statuses=statuses,
            available={service: list(NEXT[status]) for service, status in statuses.items()},
            card=(card_status(log, services, notify_deadline_passed=overdue)
                  if shows_card else CardStatus.REGISTERED),
            log=log,
            crew_options=(self._crew_options_for(services) if shows_card else []),
            crew_selected=card.crew_selected if card is not None else None,
            zone_decision=(
                next((detail == "в зоне" for action, _at, detail in reversed(card.dds_log)
                      if action == "zone.decision"), None)
                if card is not None else None
            ),
            phone_reports=list(card.phone_reports) if card is not None else [],
            phone_lines=list(card.phone_lines) if card is not None else [],
            phone_pending=card.phone_pending if card is not None else None,
            card_id=card.card_id if card is not None else None,
            card_index=(desk.card_index + 1 if card is not None
                        else min(desk.next_index + 1, len(desk.scenarios))),
            card_total=len(desk.scenarios) or 1,
            reply_text=card.reply_text if card is not None else "",
            completed_cards=[DdsCardSummary(card_id=item.card_id,
                                            scenario_id=item.scenario_id,
                                            score_auto=item.score_auto)
                             for item in desk.completed],
            queue_cards=queue_cards,
            pending_cards_count=max(
                0, len(desk.scenarios) - len(desk.completed)
                - (1 if card is not None else 0),
            ),
            next_arrival_in_seconds=(
                max(0, int((desk.next_arrival_at - now_utc()).total_seconds() + 0.999))
                if desk.next_arrival_at is not None else None
            ),
            max_waiting_cards=desk.max_waiting,
        )

    def card_received_event(self):
        """Снимок карточки для станции ДДС."""
        from app.domain.events import CardReceived

        card = self.desk.active
        return CardReceived(
            card=card.dispatched_card if card is not None else self.dispatched_card,
            from_operator=("учебный сценарий" if self.exercise is Exercise.DDS
                           or (self.handoff_to_dds and self.desk.card_index > 0)
                           else self.trainee_name or "оператор 112"),
            at=(card.dispatched_at if card is not None else self.dispatched_at) or now_utc(),
            card_index=self.desk.card_index + 1,
            card_total=len(self.desk.scenarios) or 1,
        )

    @property
    def ended(self) -> bool:
        return self.ended_at is not None

    def end(self, reason: CallEndReason) -> datetime:
        """Закрыть занятие; возвращает момент окончания для журнала."""
        self.ended_at = now_utc()
        self.end_reason = reason
        return self.ended_at

    def snapshot(self) -> SessionSnapshot:
        """Полное состояние. Монитор в классе включают посреди занятия —
        он обязан показать текущее, а не ждать следующего события."""
        card = self.shown_card()
        return SessionSnapshot(
            session_id=self.session_id,
            scenario_id=card.scenario.id if card is not None else self.scenario_id,
            scenario_title=card.scenario.title if card is not None else self.scenario_title,
            level=card.scenario.level.value if card is not None else self.level,
            mode=self.mode,
            exercise=self.exercise,
            criteria=self.criteria,
            trainee_name=self.trainee_name,
            started_at=self.started_at,
            kio=card.kio if card is not None else self.kio,
            required_fields=(list(card.scenario.required_fields) if card is not None
                             else self.required_fields),
            transcript=list(self.transcript),
            timers=self.shown_timers().snapshot(),
            hints_used=len(self.hints_shown),
            ended=self.ended,
        )
