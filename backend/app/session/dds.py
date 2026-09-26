"""Пульт ДДС: одновременно выданные учебные карточки одного занятия.

Живое состояние карточки существует в одном экземпляре — в `DdsLiveCard`.
Пульт хранит карточки и id активной; переключение меняет id и ничего не
копирует, поэтому обработчики станции пишут прямо в `desk.active`.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app.domain import ekp
from app.domain.events import Metric
from app.domain.kio import KIO, ResponseStatus, apply_patch
from app.domain.statuses import (
    PhoneCallPending,
    PhoneLineRecord,
    PhoneReportRecord,
    ServiceStatus,
    StatusEntry,
    check,
)
from app.domain.taxonomy import Finding
from app.domain.timers import TimerCode
from app.scenarios.schema import Scenario
from app.session.timers import SessionTimers, now_utc


@dataclass
class DdsCardRecord:
    card_id: UUID
    scenario_id: str
    reply_text: str
    metrics: list[Metric]
    findings: list[Finding]
    actions: list[dict[str, Any]]
    duration_ms: int
    title: str | None = None
    address: str | None = None
    description: str | None = None
    incident_type: str | None = None
    victims_count: int | None = None
    received_at: datetime | None = None
    managed_service: str | None = None
    recipient_services: list[str] = field(default_factory=list)

    @property
    def score_auto(self) -> float:
        total = sum(metric.weight for metric in self.metrics)
        passed = sum(metric.weight * (metric.credit if metric.credit is not None
                                      else float(metric.passed)) for metric in self.metrics)
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
    reply_log: list[tuple[datetime, str]] = field(default_factory=list)

    @property
    def card_id(self) -> UUID:
        return self.dispatched_card.card_id

    def on_event(self, event_type: str) -> None:
        self.timers.on_event(event_type)

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


def build_card(
    scenario: Scenario, index: int, limits: dict[TimerCode, int]
) -> DdsLiveCard:
    """Готовая карточка сценария, переданная в ДДС в момент вызова.

    Каждая карточка получает свою копию сценария, свою историю и свой таймер
    с лимитом преподавателя: 30 секунд идут с поступления, а не с открытия.
    """
    scenario = scenario.model_copy(deep=True)
    truth = scenario.ground_truth
    address_fact = next(
        (fact.value for fact in scenario.facts if "address" in fact.id), ""
    )
    caller_fact = next(
        (
            fact.value
            for fact in scenario.facts
            if fact.id in {"f_caller", "f_applicant"}
        ),
        "",
    )
    caller_phone = next(
        (
            match.group(0).strip()
            for match in re.finditer(
                r"(?<!\d)(?:\+?7|8)?(?:[\s().-]*\d){10,11}(?!\d)", caller_fact
            )
            if len(re.sub(r"\D", "", match.group(0))) in {10, 11}
        ),
        None,
    )
    caller_names = re.findall(
        r"(?<![А-ЯЁа-яё])([А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,2})(?![А-ЯЁа-яё])",
        caller_fact,
    )
    # Some ticket facts name multiple witnesses; don't silently attribute the
    # contact number to the first person when the source doesn't disambiguate.
    caller_name = caller_names[0] if len(caller_names) == 1 else None
    floor = re.search(r"(\d+)[-‑–]?й?\s*этаж", address_fact, re.IGNORECASE)
    service = truth.dds.value if truth.dds else None
    fallback = {
        "01": "Служба 101",
        "02": "МВД",
        "03": "Скорая помощь",
        "04": "Аварийная служба",
    }
    if scenario.student_card is not None:
        # A moderated learner-authored KIO is the card itself; do not flatten
        # it to title/description and silently discard its structured fields.
        kio = scenario.student_card.model_copy(
            deep=True,
            update={
                "card_id": uuid4(),
                "registered_at": now_utc(),
                "response_status": ResponseStatus.REGISTERED,
                "dispatch_order_at": None,
                "arrival_at": None,
            },
        )
    else:
        kio = KIO(
            registered_at=now_utc(),
            caller_number=caller_phone,
            caller_name=caller_name,
            caller_contact=caller_phone,
            address=truth.address or address_fact or None,
            floor=floor.group(1) if floor else None,
            incident_type=truth.incident_type,
            incident_code=truth.incident_code,
            incident_group=(
                ekp.incident(truth.incident_code).group
                if truth.incident_code and ekp.incident(truth.incident_code)
                else None
            ),
            dds=truth.dds,
            signs=list(scenario.signs),
            notify=list(truth.notify)
            or ([fallback[service]] if service in fallback else []),
            victims_count=truth.victims,
            description="; ".join(fact.value for fact in scenario.facts[:3])
            or scenario.first_line,
        )
    if service:
        kio = apply_patch(kio, {"dds": service})
    kio.response_status = ResponseStatus.TRANSFERRED
    timers = SessionTimers(limits=dict(limits))
    timers.on_event("dds.dispatch")
    return DdsLiveCard(
        original_index=index,
        scenario=scenario,
        kio=kio,
        dispatched_card=kio.model_copy(deep=True),
        dispatched_at=now_utc(),
        timers=timers,
    )


@dataclass
class DdsDesk:
    """Живые карточки занятия, активная из них и очередь поступления."""

    scenarios: list[Scenario] = field(default_factory=list)
    cards: dict[UUID, DdsLiveCard] = field(default_factory=dict)
    active_id: UUID | None = None
    #: Номер карточки на экране. После завершения последней из поступивших
    #: остаётся прежним, пока не придёт следующая.
    card_index: int = 0
    completed: list[DdsCardRecord] = field(default_factory=list)
    limits: dict[TimerCode, int] = field(default_factory=lambda: dict(SessionTimers().limits))
    arrival_interval_seconds: int = 0
    max_waiting: int = 3
    next_index: int = 0
    next_arrival_at: datetime | None = None

    @property
    def active(self) -> DdsLiveCard | None:
        return self.cards.get(self.active_id) if self.active_id is not None else None

    def ordered(self) -> list[DdsLiveCard]:
        return sorted(self.cards.values(), key=lambda card: card.original_index)

    def open(self, card_id: UUID) -> bool:
        """Сделать карточку активной; остальные продолжают идти."""
        card = self.cards.get(card_id)
        if card is None:
            return False
        self.active_id = card_id
        self.card_index = card.original_index
        return True

    def add(self, card: DdsLiveCard) -> DdsLiveCard:
        self.cards[card.card_id] = card
        return card

    def is_completed(self, card_id: UUID) -> bool:
        return any(item.card_id == card_id for item in self.completed)

    def complete(self, card_id: UUID, record: DdsCardRecord) -> DdsLiveCard | None:
        """Закрыть карточку и открыть следующую по порядку поступления.

        Если поступивших больше нет, пульт остаётся между карточками: номер
        завершённой сохраняется, пока очередь не выдаст следующую.
        """
        card = self.cards.pop(card_id)
        self.completed.append(record)
        remaining = self.ordered()
        if not remaining:
            self.active_id = None
            self.card_index = card.original_index
            return None
        following = next(
            (item for item in remaining if item.original_index > card.original_index),
            remaining[0],
        )
        self.open(following.card_id)
        return following

    def start(
        self,
        scenarios: list[Scenario],
        *,
        limits: dict[TimerCode, int],
        arrival_interval_seconds: int = 0,
        max_waiting: int = 3,
        now: datetime | None = None,
    ) -> int:
        """Начать поступление очереди с первой карточки; остальные идут по темпу."""
        self.cards.clear()
        self.active_id = None
        self.scenarios = scenarios
        self.limits = dict(limits)
        self.arrival_interval_seconds = arrival_interval_seconds
        self.max_waiting = max_waiting
        self.next_index = 0
        self.next_arrival_at = now or now_utc()
        return self.deliver_due(self.next_arrival_at)

    def deliver_due(self, now: datetime | None = None) -> int:
        """Выдать всё, чему настал срок и для чего есть место в ожидании.

        Активная карточка не считается ожидающей. Каждая карточка запускает
        собственный 30-секундный таймер ровно при поступлении.
        """
        now = now or now_utc()
        if self.next_arrival_at is None or self.next_arrival_at > now:
            return 0

        delivered = 0
        while self.next_index < len(self.scenarios):
            waiting_count = len(self.cards) - int(self.active is not None)
            if waiting_count >= self.max_waiting:
                break

            index = self.next_index
            card = self.add(build_card(self.scenarios[index], index, self.limits))
            self.next_index += 1
            delivered += 1
            if self.active is None:
                self.open(card.card_id)

            if self.next_index >= len(self.scenarios):
                self.next_arrival_at = None
                break
            if self.arrival_interval_seconds:
                # Pace from actual delivery time. If the cap was full, the next
                # card becomes due one full interval after this release.
                self.next_arrival_at = card.dispatched_at + timedelta(
                    seconds=self.arrival_interval_seconds
                )
                break
            # Zero interval means fill active + available waiting slots now.
            self.next_arrival_at = now

        return delivered


def prepare_queue(
    state,
    scenarios: list[Scenario],
    arrival_interval_seconds: int = 0,
    max_waiting: int = 3,
) -> None:
    """Упражнение ДДС: очередь готовых карточек с лимитами занятия."""
    state.desk.start(
        scenarios,
        limits=state.timers.limits,
        arrival_interval_seconds=arrival_interval_seconds,
        max_waiting=max_waiting,
    )


def prepare_handoff_queue(
    state,
    additional_scenarios: list[Scenario],
    arrival_interval_seconds: int = 0,
    max_waiting: int = 3,
) -> None:
    """Передать созданную курсантом карточку в ДДС перед готовыми карточками."""
    if state.dispatched_card is None or state.scenario is None:
        return
    now = now_utc()
    state.operator_kio = state.dispatched_card.model_copy(deep=True)
    state.operator_scenario = state.scenario.model_copy(deep=True)
    # Карточка курсанта становится первым живым объектом очереди. Её данные
    # не переписываются из эталона: именно их будет обрабатывать ДДС.
    dds_timers = SessionTimers(limits=dict(state.timers.limits))
    dds_timers.on_event("dds.dispatch")
    first = DdsLiveCard(
        original_index=0,
        scenario=state.operator_scenario,
        kio=state.operator_kio.model_copy(deep=True),
        dispatched_card=state.operator_kio.model_copy(deep=True),
        dispatched_at=state.dispatched_at or now,
        timers=dds_timers,
    )
    desk = state.desk
    desk.cards = {first.card_id: first}
    desk.scenarios = [state.operator_scenario, *additional_scenarios]
    desk.limits = dict(state.timers.limits)
    desk.arrival_interval_seconds = arrival_interval_seconds
    desk.max_waiting = max_waiting
    desk.next_index = 1
    desk.next_arrival_at = (
        (
            now + timedelta(seconds=arrival_interval_seconds)
            if additional_scenarios and arrival_interval_seconds
            else now
        )
        if additional_scenarios
        else None
    )
    desk.open(first.card_id)
    desk.deliver_due(now)
