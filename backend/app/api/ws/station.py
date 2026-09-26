"""Сокет диспетчера ДДС.

Учебный исходящий звонок бригаде имитируется событиями JSON. Для целевого
локального прототипа не нужна внешняя телефония или аудиомодель.

Самая ценная механика цепочки — `card.bounce`: диспетчер видит, что не указан
этаж, и отбивает карточку обратно. Неполнота КИО перестаёт быть процентом
в отчёте и становится сорванным выездом с конкретной причиной.
"""

import asyncio
import logging
import re
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.api.auth import principal_of, websocket_origin_allowed
from app.domain.events import (
    CallEndReason,
    CommandAck,
    ErrorEvent,
    ErrorKind,
    Exercise,
    PhoneLine,
    PhoneReport,
    ScoreReady,
    SessionEnded,
    StationState,
    StationToServer,
)
from app.domain.roles import Role
from app.domain.statuses import (
    PRIMARY,
    SERVICE_STATUS_LABELS,
    PhoneCallPending,
    PhoneLineRecord,
    PhoneReportRecord,
    ServiceStatus,
    StatusError,
    current,
)
from app.domain.timers import TimerCode
from app.scoring.address import address_matches
from app.session.dds import DdsLiveCard
from app.session.finish import finish, score_dds_card
from app.session.hub import LEASE_FENCED_MESSAGE, hub
from app.session.state import now_utc

log = logging.getLogger(__name__)
router = APIRouter()

_adapter = TypeAdapter(StationToServer)


def _start_dds_work_timer(card: DdsLiveCard) -> None:
    """Start the three-minute work clock once, when the card is opened."""
    timer = card.timers.timers.get(TimerCode.DDS_WORK)
    if timer is None or timer.started_at is None:
        card.on_event("dds.open")


#: Команды, которым не нужна активная карточка пульта.
CARDLESS_EVENTS = {"card.open", "station.finish", "card.bounce"}

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
def _error(session_id: UUID, message: str) -> None:
    hub.to_station(session_id, ErrorEvent(code=ErrorKind.UNSUPPORTED_EVENT, message=message))


def _line(session_id: UUID, card: DdsLiveCard, speaker: str, text: str) -> None:
    call = card.phone_pending
    if call is None:
        return
    line = PhoneLineRecord(service=call.service, crew=call.crew,
                           speaker=speaker, text=text, at=now_utc())
    card.phone_lines.append(line)
    card.dds_log.append((f"phone.line.{speaker}", line.at, f"{call.crew}: {text}"))
    hub.to_station(session_id, PhoneLine(**line.model_dump()))


def _address_matches(expected: str | None, supplied: str) -> bool:
    """Не даём сообщить бригаде другой номер дома/другую улицу."""
    return address_matches(expected, supplied)


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


def _finish_phone_call(session_id: UUID, card: DdsLiveCard) -> None:
    call = card.phone_pending
    assert call is not None
    text = _report_text(call.phase, call.crew, card.dispatched_card.address or "из карточки")
    _line(session_id, card, "crew", text)
    report = PhoneReportRecord(service=call.service, crew=call.crew,
                               phase=call.phase, text=text, at=now_utc())
    card.phone_reports.append(report)
    card.dds_log.append(("phone.report", now_utc(), f"{call.crew}: {call.phase}"))
    card.phone_pending = None
    hub.to_station(session_id, PhoneReport(**report.model_dump()))


async def _finish_dds(session_id: UUID, state) -> None:
    state.ended_at = now_utc()
    state.end_reason = CallEndReason.COMPLETE
    for card in state.desk.cards.values():
        timer = card.timers.timers.get(TimerCode.DDS_WORK)
        if timer is not None and timer.started_at is not None:
            card.timers.on_event("dds.finish")
    hub.stop_ticker(session_id)
    hub.to_station(session_id, SessionEnded(reason=CallEndReason.COMPLETE))
    hub.to_observers(session_id, SessionEnded(reason=CallEndReason.COMPLETE))
    if hub.journal:
        await hub.journal.session_ended(session_id, state.ended_at, CallEndReason.COMPLETE.value)
    await finish(session_id, state)
    hub.to_station(session_id, ScoreReady(session_id=session_id))


async def _handle(session_id: UUID, state, event) -> None:
    if state.ended:
        _error(session_id, "Занятие уже завершено")
        return
    card = state.desk.active
    if card is None and event.type not in CARDLESS_EVENTS:
        _error(session_id, "Нет активной карточки")
        return
    match event.type:
        case "card.ack":
            # Подтверждение приёма — это статус «Принята» у главной службы.
            # Кнопка осталась ради живой цепочки 112 → ДДС (lct-20), где
            # диспетчер один и выбирать службу не из чего.
            if not event.comment.strip():
                _error(session_id, "Для подтверждения приёма добавьте комментарий с основанием")
                return
            if any(action == "card.ack" for action, _at, _detail in card.dds_log):
                return
            card.on_event("card.ack")
            card.dds_log.append(("card.ack", now_utc(), None))
            services = state.card_services(card)
            if services:
                _start_dds_work_timer(card)
                try:
                    card.set_service_status(
                        services[0], ServiceStatus.ACCEPTED, event.comment, author="диспетчер"
                    )
                except StatusError:
                    pass  # статус уже стоит: повторное нажатие ничего не меняет
        case "card.status":
            if event.service not in state.card_services(card):
                _error(session_id, "Можно менять статусы только своей ДДС")
                return
            _start_dds_work_timer(card)
            try:
                card.set_service_status(
                    event.service, event.status, event.comment, author="диспетчер"
                )
            except StatusError as exc:
                hub.to_station(
                    session_id,
                    ErrorEvent(code=ErrorKind.UNSUPPORTED_EVENT, message=str(exc)),
                )
                return
            # Первичный статус останавливает норматив 30 секунд.
            if event.status in PRIMARY:
                card.on_event("card.ack")
            if event.status in {
                ServiceStatus.COMPLETED, ServiceStatus.DECLINED, ServiceStatus.REFUSED,
            }:
                card.on_event("dds.complete")
        case "crew.select":
            if event.crew not in state.crew_options(card):
                _error(session_id, "Выберите бригаду из списка доступных")
                return
            service = state.crew_service(card, event.crew)
            assigned = card.crew_assignments.get(service)
            if assigned and assigned != event.crew and any(
                report.service == service for report in card.phone_reports
            ):
                _error(session_id, "После первого доклада бригаду этой службы менять нельзя")
                return
            if card.phone_pending is not None:
                _error(session_id, "Завершите текущий разговор перед сменой бригады")
                return
            if card.crew_selected == event.crew and assigned == event.crew:
                return
            card.crew_selected = event.crew
            card.crew_assignments[service] = event.crew
            card.dds_log.append(("crew.select", now_utc(), event.crew))
        case "phone.dial":
            if card.phone_pending is not None:
                _error(session_id, "Разговор уже идёт: передайте сведения или завершите звонок")
                return
            crew = card.crew_selected
            service = state.crew_service(card, crew) if crew else None
            if service is None:
                _error(session_id, "Сначала выберите бригаду")
                return
            previous = [report for report in card.phone_reports if report.service == service]
            if len(previous) >= len(REPORT_PHASES):
                _error(session_id, "Все доклады этой бригады уже получены")
                return
            phase = REPORT_PHASES[len(previous)]
            required = REQUIRED_STATUS[phase]
            if current(card.status_log, service) not in STATUS_AT_OR_AFTER[phase]:
                _error(session_id, f"Перед звонком нужен статус «{SERVICE_STATUS_LABELS[required]}» этой службы")
                return
            card.phone_pending = PhoneCallPending(service=service, crew=crew, phase=phase)
            card.dds_log.append(("phone.dial", now_utc(), crew))
            greeting = (f"{crew}, старший группы на связи. Назовите адрес, характер происшествия "
                        "и что требуется от бригады."
                        if phase == "dispatched" else
                        f"{crew}, старший группы на связи. Слушаю ваш запрос по карточке.")
            _line(session_id, card, "crew", greeting)
        case "phone.brief":
            call = card.phone_pending
            if call is None or call.phase != "dispatched":
                _error(session_id, "Сначала соединитесь со старшим группы для передачи вызова")
                return
            if not _address_matches(card.dispatched_card.address, event.address):
                _error(session_id, "Проверьте адрес: улица и номер дома должны совпадать с карточкой")
                return
            if not _incident_matches(card, event.incident):
                _error(session_id, "Уточните характер происшествия по данным карточки")
                return
            if not _has_purpose(event.request, ("выезд", "выех", "направ", "прибыт",
                                                "реагир", "помощ", "подтверд", "долож")):
                _error(session_id, "Сформулируйте задачу: выезд, помощь или доклад бригады")
                return
            _line(session_id, card, "dispatcher", f"Адрес: {event.address.strip()}. "
                  f"Происшествие: {event.incident.strip()}. {event.request.strip()}")
            _finish_phone_call(session_id, card)
        case "phone.check":
            call = card.phone_pending
            if call is None or call.phase == "dispatched":
                _error(session_id, "Сначала передайте вызов, затем запросите обстановку")
                return
            if not _has_purpose(event.text, ("обстанов", "статус", "прибыл", "доех",
                                              "выех", "работ", "заверш", "мест",
                                              "ход", "сообщ", "долож", "уточн")):
                _error(session_id, "Спросите обстановку, прибытие или ход работ по карточке")
                return
            _line(session_id, card, "dispatcher", event.text.strip())
            _finish_phone_call(session_id, card)
        case "phone.hangup":
            if card.phone_pending is None:
                _error(session_id, "Нет активного разговора")
                return
            card.dds_log.append(("phone.hangup", now_utc(), card.phone_pending.crew))
            card.phone_pending = None
        case "card.reply":
            if (state.exercise is not Exercise.DDS and not state.handoff_to_dds) or (
                event.card_id != card.card_id
            ):
                _error(session_id, "Ответ относится не к текущей карточке")
                return
            # A browser may lose the acknowledgement after the server has
            # committed this replace-style value. Reconnect retries are safe:
            # don't create another journal row
            # when the current card already contains exactly this text.
            if card.reply_text == event.text:
                return
            card.reply_text = event.text
            card.reply_log.append((now_utc(), event.text))
        case "card.open":
            if (state.exercise is not Exercise.DDS and not state.handoff_to_dds) or not state.desk.open(event.card_id):
                _error(session_id, "Карточка отсутствует в текущей очереди")
                return
            _start_dds_work_timer(state.desk.active)
            hub.to_station(session_id, state.card_received_event())
            # CardReceived carries the contents, while StationState carries
            # the status journal and current queue. Send both on every switch
            # so the newly opened card cannot briefly inherit the previous
            # card's status snapshot until the next periodic tick.
            hub.to_station(session_id, StationState(snapshot=state.station_snapshot()))
        case "card.next":
            if (state.exercise is not Exercise.DDS and not state.handoff_to_dds) or (
                event.card_id != card.card_id
            ):
                _error(session_id, "Следующая карточка недоступна: ID текущей не совпадает")
                return
            if state.desk.is_completed(event.card_id):
                _error(session_id, "Эта карточка уже завершена")
                return
            # Keep the lesson alive if selected cards have not arrived yet.
            # The next delivery may become the active card immediately or
            # after its configured interval; no completed card is reused.
            if state.desk.complete(card.card_id, score_dds_card(state, card)) is not None:
                hub.to_station(session_id, state.card_received_event())
            active_before_delivery = state.desk.active_id
            state.desk.deliver_due()
            if state.desk.active_id and state.desk.active_id != active_before_delivery:
                hub.to_station(session_id, state.card_received_event())
            hub.to_station(session_id, StationState(snapshot=state.station_snapshot()))
            if not state.desk.cards and len(state.desk.completed) >= len(state.desk.scenarios):
                await _finish_dds(session_id, state)
        case "station.finish":
            if state.exercise is not Exercise.DDS and not state.handoff_to_dds:
                _error(session_id, "Операторское занятие завершается после звонка 112")
                return
            await _finish_dds(session_id, state)
        case "card.bounce":
            _error(
                session_id,
                "ДДС не проверяет заполнение карточки: замечания передаёт служба контроля 112",
            )
            return
        case "zone.decision":
            previous = next(
                (detail for action, _at, detail in reversed(card.dds_log)
                 if action == "zone.decision"),
                None,
            )
            decision = "в зоне" if event.in_zone else "не в зоне"
            if previous is not None:
                if previous != decision:
                    _error(session_id, "Решение по зоне уже записано для этой карточки")
                return
            card.on_event("zone.decision")
            card.dds_log.append(("zone.decision", now_utc(), decision))
        case "crew.dispatched":
            card.kio = card.kio.model_copy(update={"dispatch_order_at": event.at})
            card.dds_log.append(("crew.dispatched", now_utc(), None))
        case "crew.arrived":
            card.on_event("crew.arrived")
            card.kio = card.kio.model_copy(update={"arrival_at": event.at})
            card.dds_log.append(("crew.arrived", now_utc(), None))
    await hub.checkpoint(session_id)
    hub.to_station(session_id, StationState(snapshot=state.station_snapshot()))
    hub.to_observers(session_id, state.snapshot())


async def _pump(ws: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        event = await queue.get()
        await ws.send_text(event.model_dump_json())
        if (isinstance(event, ErrorEvent) and event.code is ErrorKind.INTERNAL
                and event.message == LEASE_FENCED_MESSAGE):
            await ws.close(code=1012)
            return


async def _reject(ws: WebSocket, message: str) -> None:
    """Отказ до входа в цикл: сокет закрывается с объяснением, а не молча."""
    await ws.send_text(
        ErrorEvent(code=ErrorKind.FORBIDDEN, message=message).model_dump_json()
    )
    await ws.close()


@router.websocket("/ws/station/{session_id}")
async def station(ws: WebSocket, session_id: UUID, role: str = "dds") -> None:
    if not websocket_origin_allowed(ws):
        await ws.close(code=1008)
        return
    if hub.is_lease_fenced(session_id):
        await ws.close(code=1012)
        return
    await ws.accept()

    # За АРМ ДДС садится обучающийся, преподаватель смотрит и подменяет.
    who = principal_of(ws)
    if who is None or who.role not in (Role.INSTRUCTOR, Role.TRAINEE):
        await _reject(ws, "Недостаточно прав для этого экрана")
        return

    state = hub.get(session_id)
    if state is None:
        await ws.send_text(
            ErrorEvent(code=ErrorKind.SESSION_NOT_FOUND, message="Занятие не запущено").model_dump_json()
        )
        await ws.close()
        return
    if who.role is Role.INSTRUCTOR and state.owner_login != who.login:
        await ws.send_text(
            ErrorEvent(code=ErrorKind.SESSION_NOT_FOUND, message="Занятие не запущено").model_dump_json()
        )
        await ws.close()
        return
    if who.role is Role.TRAINEE and (
        state.trainee_id is None or state.trainee_id != who.trainee_id
    ):
        await _reject(ws, "Занятие не назначено этому обучающемуся")
        return

    with hub.station(session_id) as queue:
        sender = asyncio.create_task(_pump(ws, queue))
        try:
            # Карточка, переданная до подключения станции, не теряется:
            # диспетчер садится за АРМ, когда вызов уже идёт.
            if state.desk.active is not None:
                hub.to_station(session_id, state.card_received_event())
                hub.to_station(session_id, StationState(snapshot=state.station_snapshot()))
            while True:
                payload = await ws.receive_json()
                try:
                    event = _adapter.validate_python(payload)
                except ValidationError:
                    hub.to_station(
                        session_id,
                        ErrorEvent(code=ErrorKind.UNSUPPORTED_EVENT, message=str(payload)[:200]),
                    )
                    continue
                raw_command_id = payload.get("_command_id") if isinstance(payload, dict) else None
                try:
                    command_id = UUID(raw_command_id) if raw_command_id is not None else None
                except (ValueError, TypeError, AttributeError):
                    hub.to_station(
                        session_id,
                        ErrorEvent(code=ErrorKind.UNSUPPORTED_EVENT,
                                   message="Некорректный идентификатор команды"),
                    )
                    continue
                if command_id is not None and str(command_id) in state.processed_station_commands:
                    # The checkpoint already proves this exact command committed.
                    # Re-ack it without rerunning its business transition.
                    hub.to_station(session_id, CommandAck(command_id=command_id))
                    continue
                async with hub.durable_transition(session_id):
                    await _handle(session_id, state, event)
                    if command_id is not None:
                        state.processed_station_commands.append(str(command_id))
                        del state.processed_station_commands[:-512]
                        # Commit the state+dedupe ID before acknowledging. The
                        # transition context can have already flushed other
                        # events; an explicit checkpoint here makes the
                        # command/ACK boundary independent of that batch state.
                        await hub.checkpoint(session_id)
                        # The hub stages non-error events until the checkpoint
                        # transaction has committed, including this ack.
                        hub.to_station(session_id, CommandAck(command_id=command_id))
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001 — failed durable transition may fence the owner
            if not state.lease_fenced:
                raise
            log.info(
                "закрытие станционного WebSocket после fencing занятия %s",
                session_id,
            )
            # `hub.checkpoint` broadcasts a structured fence event before
            # propagating the failed database write. Let the sender deliver
            # that event and close with 1012 instead of an opaque 1006.
            await asyncio.gather(sender, return_exceptions=True)
            return
        finally:
            sender.cancel()
