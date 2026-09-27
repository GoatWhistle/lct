"""Пульт ДДС: одновременно выданные учебные карточки одного занятия.

Живое состояние карточки существует в одном экземпляре — в `DdsLiveCard`.
Пульт хранит карточки и id активной; переключение меняет id и ничего не
копирует. Правила команд станции — `DdsDesk.apply`: сокет только разбирает
команду, сохраняет итог и рассылает события.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.domain import ekp
from app.domain.events import Exercise, Metric, PhoneLine, PhoneReport, StationState
from app.domain.kio import KIO, ResponseStatus, apply_patch
from app.domain.statuses import (
    PRIMARY,
    SERVICE_STATUS_LABELS,
    PhoneCallPending,
    PhoneLineRecord,
    PhoneReportRecord,
    ServiceStatus,
    StatusEntry,
    StatusError,
    check,
    current,
)
from app.domain.taxonomy import Finding
from app.domain.timers import TimerCode
from app.scenarios.schema import Scenario
from app.scoring.address import address_matches
from app.session.timers import SessionTimers, now_utc

if TYPE_CHECKING:
    from app.session.state import SessionState


class DdsCardRecord(BaseModel):
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
    recipient_services: list[str] = Field(default_factory=list)

    @property
    def score_auto(self) -> float:
        total = sum(metric.weight for metric in self.metrics)
        passed = sum(metric.weight * (metric.credit if metric.credit is not None
                                      else float(metric.passed)) for metric in self.metrics)
        return round(100 * passed / total, 1) if total else 0.0


class DdsLiveCard(BaseModel):
    """Изолированное живое состояние одной одновременно выданной карточки."""

    original_index: int
    scenario: Scenario
    kio: KIO
    dispatched_card: KIO
    dispatched_at: datetime
    timers: SessionTimers
    bounced_fields: list[str] = Field(default_factory=list)
    dds_log: list[tuple[str, datetime, str | None]] = Field(default_factory=list)
    status_log: list[StatusEntry] = Field(default_factory=list)
    crew_selected: str | None = None
    crew_assignments: dict[str, str] = Field(default_factory=dict)
    phone_reports: list[PhoneReportRecord] = Field(default_factory=list)
    phone_lines: list[PhoneLineRecord] = Field(default_factory=list)
    phone_pending: PhoneCallPending | None = None
    reply_text: str = ""
    reply_log: list[tuple[datetime, str]] = Field(default_factory=list)

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


#: Команды, которым не нужна активная карточка пульта.
CARDLESS_COMMANDS = {"card.open", "station.finish", "card.bounce"}

#: Доклады бригады идут строго по порядку; перед каждым звонком служба
#: должна стоять не раньше нужного статуса.
REPORT_PHASES = ("dispatched", "arrived", "working", "completed")
REQUIRED_STATUS = {
    "dispatched": ServiceStatus.ACCEPTED,
    "arrived": ServiceStatus.RESPONDING,
    "working": ServiceStatus.ARRIVED,
    "completed": ServiceStatus.WORKING,
}
STATUS_AT_OR_AFTER = {
    "dispatched": {ServiceStatus.ACCEPTED, ServiceStatus.RESPONDING,
                   ServiceStatus.ARRIVED, ServiceStatus.WORKING},
    "arrived": {ServiceStatus.RESPONDING, ServiceStatus.ARRIVED, ServiceStatus.WORKING},
    "working": {ServiceStatus.ARRIVED, ServiceStatus.WORKING},
    "completed": {ServiceStatus.WORKING, ServiceStatus.COMPLETED},
}


@dataclass
class DeskOutcome:
    """Итог команды пульта. Отказ ничего не меняет и событий не несёт."""

    #: События станции в порядке отправки.
    events: list[Any] = field(default_factory=list)
    error: str | None = None
    #: Состояние изменилось: транспорт сохраняет его и шлёт свежий снимок.
    #: Повтор уже учтённой команды проходит молча, без записи.
    changed: bool = False
    #: Занятие ДДС дошло до конца; завершает его сессия, а не пульт.
    finished: bool = False


def _refused(message: str) -> DeskOutcome:
    return DeskOutcome(error=message)


def _start_work_timer(card: DdsLiveCard) -> None:
    """Три минуты на работу идут один раз — с открытия карточки."""
    timer = card.timers.timers.get(TimerCode.DDS_WORK)
    if timer is None or timer.started_at is None:
        card.on_event("dds.open")


def _line(card: DdsLiveCard, speaker: str, text: str) -> PhoneLine | None:
    call = card.phone_pending
    if call is None:
        return None
    line = PhoneLineRecord(service=call.service, crew=call.crew,
                           speaker=speaker, text=text, at=now_utc())
    card.phone_lines.append(line)
    card.dds_log.append((f"phone.line.{speaker}", line.at, f"{call.crew}: {text}"))
    return PhoneLine(**line.model_dump())


def _incident_matches(card: DdsLiveCard, supplied: str) -> bool:
    text = supplied.casefold()
    # Описание часто начинается с адреса: его нельзя считать совпадением
    # характера происшествия. Берём только название сценария и признаки ЕКП.
    source = " ".join((card.scenario.title, " ".join(card.kio.signs)))
    anchors = {word[:4] for word in re.findall(r"[а-яё]{5,}", source.casefold())}
    return len(supplied.strip()) >= 8 and any(anchor in text for anchor in anchors)


def _has_purpose(text: str, stems: tuple[str, ...]) -> bool:
    normalized = text.casefold()
    return len(text.strip()) >= 8 and any(stem in normalized for stem in stems)


def _report_text(phase: str, crew: str, address: str) -> str:
    match phase:
        case "dispatched":
            return f"{crew}: вызов по адресу {address} принят, выезжаем. О прибытии доложу."
        case "arrived":
            return f"{crew}: прибыли по адресу {address}. Уточняем обстановку на месте."
        case "working":
            return f"{crew}: обстановка уточнена, приступили к работам. Сообщим о завершении."
        case _:
            return f"{crew}: работы завершены. Дальнейшая помощь от нашей бригады не требуется."


def _finish_phone_call(card: DdsLiveCard) -> list[Any]:
    call = card.phone_pending
    assert call is not None
    text = _report_text(call.phase, call.crew, card.dispatched_card.address or "из карточки")
    line = _line(card, "crew", text)
    report = PhoneReportRecord(service=call.service, crew=call.crew,
                               phase=call.phase, text=text, at=now_utc())
    card.phone_reports.append(report)
    card.dds_log.append(("phone.report", now_utc(), f"{call.crew}: {call.phase}"))
    card.phone_pending = None
    return [line, PhoneReport(**report.model_dump())]


class DdsDesk(BaseModel):
    """Живые карточки занятия, активная из них и очередь поступления."""

    scenarios: list[Scenario] = Field(default_factory=list)
    cards: dict[UUID, DdsLiveCard] = Field(default_factory=dict)
    active_id: UUID | None = None
    #: Номер карточки на экране. После завершения последней из поступивших
    #: остаётся прежним, пока не придёт следующая.
    card_index: int = 0
    completed: list[DdsCardRecord] = Field(default_factory=list)
    limits: dict[TimerCode, int] = Field(default_factory=lambda: dict(SessionTimers().limits))
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

    def apply(self, command: Any, session: "SessionState") -> DeskOutcome:
        """Применить команду станции к пульту: правила карточки без транспорта.

        Из сессии берутся упражнение, службы обучающегося и снимки для
        станции. Отказ возвращается до первой записи — состояние не меняется.
        """
        if session.ended:
            return _refused("Занятие уже завершено")
        card = self.active
        if card is None and command.type not in CARDLESS_COMMANDS:
            return _refused("Нет активной карточки")
        dds_lesson = session.exercise is Exercise.DDS or session.handoff_to_dds
        outcome = DeskOutcome(changed=True)
        match command.type:
            case "card.ack":
                # Подтверждение приёма — это статус «Принята» у главной службы.
                # Кнопка осталась ради живой цепочки 112 → ДДС (lct-20), где
                # диспетчер один и выбирать службу не из чего.
                if not command.comment.strip():
                    return _refused("Для подтверждения приёма добавьте комментарий с основанием")
                if any(action == "card.ack" for action, _at, _detail in card.dds_log):
                    return DeskOutcome()
                card.on_event("card.ack")
                card.dds_log.append(("card.ack", now_utc(), None))
                services = session.card_services(card)
                if services:
                    _start_work_timer(card)
                    try:
                        card.set_service_status(
                            services[0], ServiceStatus.ACCEPTED, command.comment,
                            author="диспетчер",
                        )
                    except StatusError:
                        pass  # статус уже стоит: повторное нажатие ничего не меняет
            case "card.status":
                if command.service not in session.card_services(card):
                    return _refused("Можно менять статусы только своей ДДС")
                try:
                    check(card.status_log, command.service, command.status, command.comment)
                except StatusError as exc:
                    return _refused(str(exc))
                _start_work_timer(card)
                card.set_service_status(
                    command.service, command.status, command.comment, author="диспетчер"
                )
                # Первичный статус останавливает норматив 30 секунд.
                if command.status in PRIMARY:
                    card.on_event("card.ack")
                if command.status in {
                    ServiceStatus.COMPLETED, ServiceStatus.DECLINED, ServiceStatus.REFUSED,
                }:
                    card.on_event("dds.complete")
            case "crew.select":
                if command.crew not in session.crew_options(card):
                    return _refused("Выберите бригаду из списка доступных")
                service = session.crew_service(card, command.crew)
                assigned = card.crew_assignments.get(service)
                if assigned and assigned != command.crew and any(
                    report.service == service for report in card.phone_reports
                ):
                    return _refused("После первого доклада бригаду этой службы менять нельзя")
                if card.phone_pending is not None:
                    return _refused("Завершите текущий разговор перед сменой бригады")
                if card.crew_selected == command.crew and assigned == command.crew:
                    return DeskOutcome()
                card.crew_selected = command.crew
                card.crew_assignments[service] = command.crew
                card.dds_log.append(("crew.select", now_utc(), command.crew))
            case "phone.dial":
                if card.phone_pending is not None:
                    return _refused("Разговор уже идёт: передайте сведения или завершите звонок")
                crew = card.crew_selected
                service = session.crew_service(card, crew) if crew else None
                if service is None:
                    return _refused("Сначала выберите бригаду")
                previous = [report for report in card.phone_reports if report.service == service]
                if len(previous) >= len(REPORT_PHASES):
                    return _refused("Все доклады этой бригады уже получены")
                phase = REPORT_PHASES[len(previous)]
                required = REQUIRED_STATUS[phase]
                if current(card.status_log, service) not in STATUS_AT_OR_AFTER[phase]:
                    return _refused(
                        f"Перед звонком нужен статус «{SERVICE_STATUS_LABELS[required]}» этой службы"
                    )
                card.phone_pending = PhoneCallPending(service=service, crew=crew, phase=phase)
                card.dds_log.append(("phone.dial", now_utc(), crew))
                greeting = (f"{crew}, старший группы на связи. Назовите адрес, характер происшествия "
                            "и что требуется от бригады."
                            if phase == "dispatched" else
                            f"{crew}, старший группы на связи. Слушаю ваш запрос по карточке.")
                outcome.events.append(_line(card, "crew", greeting))
            case "phone.brief":
                call = card.phone_pending
                if call is None or call.phase != "dispatched":
                    return _refused("Сначала соединитесь со старшим группы для передачи вызова")
                if not address_matches(card.dispatched_card.address, command.address):
                    return _refused("Проверьте адрес: улица и номер дома должны совпадать с карточкой")
                if not _incident_matches(card, command.incident):
                    return _refused("Уточните характер происшествия по данным карточки")
                if not _has_purpose(command.request, ("выезд", "выех", "направ", "прибыт",
                                                      "реагир", "помощ", "подтверд", "долож")):
                    return _refused("Сформулируйте задачу: выезд, помощь или доклад бригады")
                outcome.events.append(_line(
                    card, "dispatcher", f"Адрес: {command.address.strip()}. "
                    f"Происшествие: {command.incident.strip()}. {command.request.strip()}"))
                outcome.events.extend(_finish_phone_call(card))
            case "phone.check":
                call = card.phone_pending
                if call is None or call.phase == "dispatched":
                    return _refused("Сначала передайте вызов, затем запросите обстановку")
                if not _has_purpose(command.text, ("обстанов", "статус", "прибыл", "доех",
                                                   "выех", "работ", "заверш", "мест",
                                                   "ход", "сообщ", "долож", "уточн")):
                    return _refused("Спросите обстановку, прибытие или ход работ по карточке")
                outcome.events.append(_line(card, "dispatcher", command.text.strip()))
                outcome.events.extend(_finish_phone_call(card))
            case "phone.hangup":
                if card.phone_pending is None:
                    return _refused("Нет активного разговора")
                card.dds_log.append(("phone.hangup", now_utc(), card.phone_pending.crew))
                card.phone_pending = None
            case "card.reply":
                if not dds_lesson or command.card_id != card.card_id:
                    return _refused("Ответ относится не к текущей карточке")
                # A browser may lose the acknowledgement after the server has
                # committed this replace-style value. Reconnect retries are safe:
                # don't create another journal row
                # when the current card already contains exactly this text.
                if card.reply_text == command.text:
                    return DeskOutcome()
                card.reply_text = command.text
                card.reply_log.append((now_utc(), command.text))
            case "card.open":
                if not dds_lesson or not self.open(command.card_id):
                    return _refused("Карточка отсутствует в текущей очереди")
                _start_work_timer(self.active)
                # CardReceived carries the contents, while StationState carries
                # the status journal and current queue. Send both on every switch
                # so the newly opened card cannot briefly inherit the previous
                # card's status snapshot until the next periodic tick.
                outcome.events += [session.card_received_event(),
                                   StationState(snapshot=session.station_snapshot())]
            case "card.next":
                if not dds_lesson or command.card_id != card.card_id:
                    return _refused("Следующая карточка недоступна: ID текущей не совпадает")
                if self.is_completed(command.card_id):
                    return _refused("Эта карточка уже завершена")
                # Оценка живёт в модуле завершения, который сам зависит от пульта.
                from app.session.finish import score_dds_card

                # Keep the lesson alive if selected cards have not arrived yet.
                # The next delivery may become the active card immediately or
                # after its configured interval; no completed card is reused.
                if self.complete(card.card_id, score_dds_card(session, card)) is not None:
                    outcome.events.append(session.card_received_event())
                active_before_delivery = self.active_id
                self.deliver_due()
                if self.active_id and self.active_id != active_before_delivery:
                    outcome.events.append(session.card_received_event())
                outcome.events.append(StationState(snapshot=session.station_snapshot()))
                if not self.cards and len(self.completed) >= len(self.scenarios):
                    self.finish_work()
                    outcome.finished = True
            case "station.finish":
                if not dds_lesson:
                    return _refused("Операторское занятие завершается после звонка 112")
                self.finish_work()
                outcome.finished = True
            case "card.bounce":
                return _refused(
                    "ДДС не проверяет заполнение карточки: замечания передаёт служба контроля 112"
                )
            case "zone.decision":
                previous = next(
                    (detail for action, _at, detail in reversed(card.dds_log)
                     if action == "zone.decision"),
                    None,
                )
                decision = "в зоне" if command.in_zone else "не в зоне"
                if previous is not None:
                    if previous != decision:
                        return _refused("Решение по зоне уже записано для этой карточки")
                    return DeskOutcome()
                card.on_event("zone.decision")
                card.dds_log.append(("zone.decision", now_utc(), decision))
            case "crew.dispatched":
                card.kio = card.kio.model_copy(update={"dispatch_order_at": command.at})
                card.dds_log.append(("crew.dispatched", now_utc(), None))
            case "crew.arrived":
                card.on_event("crew.arrived")
                card.kio = card.kio.model_copy(update={"arrival_at": command.at})
                card.dds_log.append(("crew.arrived", now_utc(), None))
        return outcome

    def finish_work(self) -> None:
        """Конец занятия останавливает трёхминутный таймер у каждой начатой карточки."""
        for card in self.cards.values():
            timer = card.timers.timers.get(TimerCode.DDS_WORK)
            if timer is not None and timer.started_at is not None:
                card.timers.on_event("dds.finish")


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
