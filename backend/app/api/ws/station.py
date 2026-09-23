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

from app.api.auth import principal_of
from app.domain.events import (
    CallEndReason,
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
from app.scoring.grammar import assess
from app.session.dds import deliver_due_cards
from app.session.finish import finish, score_current_dds
from app.session.hub import hub
from app.session.state import now_utc

log = logging.getLogger(__name__)
router = APIRouter()

_adapter = TypeAdapter(StationToServer)

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


def _line(session_id: UUID, state, speaker: str, text: str) -> None:
    call = state.phone_pending
    if call is None:
        return
    line = PhoneLineRecord(service=call.service, crew=call.crew,
                           speaker=speaker, text=text, at=now_utc())
    state.phone_lines.append(line)
    state.dds_log.append((f"phone.line.{speaker}", line.at, f"{call.crew}: {text}"))
    hub.to_station(session_id, PhoneLine(**line.model_dump()))


def _address_matches(expected: str | None, supplied: str) -> bool:
    """Не даём сообщить бригаде другой номер дома/другую улицу."""
    if not expected:
        return bool(supplied.strip())
    numbers = re.findall(r"\d+", expected)
    spoken_numbers = re.findall(r"\d+", supplied)
    words = re.findall(r"[а-яё]{4,}", expected.casefold())
    spoken_words = re.findall(r"[а-яё]{4,}", supplied.casefold())
    return (all(number in spoken_numbers for number in numbers)
            and any(word[:4] == spoken[:4] for word in words for spoken in spoken_words))


def _incident_matches(state, supplied: str) -> bool:
    text = supplied.casefold()
    # Описание часто начинается с адреса: его нельзя считать совпадением
    # характера происшествия. Берём только название сценария и признаки ЕКП.
    source = " ".join((state.scenario_title, " ".join(state.kio.signs)))
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


def _finish_phone_call(session_id: UUID, state) -> None:
    call = state.phone_pending
    assert call is not None
    text = _report_text(call.phase, call.crew, state.dispatched_card.address or "из карточки")
    _line(session_id, state, "crew", text)
    report = PhoneReportRecord(service=call.service, crew=call.crew,
                               phase=call.phase, text=text, at=now_utc())
    state.phone_reports.append(report)
    state.dds_log.append(("phone.report", now_utc(), f"{call.crew}: {call.phase}"))
    state.phone_pending = None
    hub.to_station(session_id, PhoneReport(**report.model_dump()))


async def _finish_dds(session_id: UUID, state) -> None:
    state.ended_at = now_utc()
    state.end_reason = CallEndReason.COMPLETE
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
    match event.type:
        case "card.ack":
            # Подтверждение приёма — это статус «Принята» у главной службы.
            # Кнопка осталась ради живой цепочки 112 → ДДС (lct-20), где
            # диспетчер один и выбирать службу не из чего.
            state.on_event("card.ack")
            state.dds_log.append(("card.ack", now_utc(), None))
            services = state.managed_services()
            if services:
                try:
                    state.set_service_status(services[0], ServiceStatus.ACCEPTED)
                except StatusError:
                    pass  # статус уже стоит: повторное нажатие ничего не меняет
        case "card.status":
            if event.service not in state.managed_services():
                _error(session_id, "Можно менять статусы только своей ДДС")
                return
            try:
                state.set_service_status(
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
                state.on_event("card.ack")
        case "crew.select":
            if event.crew not in state.crew_options():
                _error(session_id, "Выберите бригаду из списка доступных")
                return
            service = state.crew_service(event.crew)
            assigned = state.crew_assignments.get(service)
            if assigned and assigned != event.crew and any(
                report.service == service for report in state.phone_reports
            ):
                _error(session_id, "После первого доклада бригаду этой службы менять нельзя")
                return
            if state.phone_pending is not None:
                _error(session_id, "Завершите текущий разговор перед сменой бригады")
                return
            state.crew_selected = event.crew
            state.crew_assignments[service] = event.crew
            state.dds_log.append(("crew.select", now_utc(), event.crew))
        case "phone.dial":
            if state.phone_pending is not None:
                _error(session_id, "Разговор уже идёт: передайте сведения или завершите звонок")
                return
            crew = state.crew_selected
            service = state.crew_service(crew) if crew else None
            if service is None:
                _error(session_id, "Сначала выберите бригаду")
                return
            previous = [report for report in state.phone_reports if report.service == service]
            if len(previous) >= len(REPORT_PHASES):
                _error(session_id, "Все доклады этой бригады уже получены")
                return
            phase = REPORT_PHASES[len(previous)]
            required = REQUIRED_STATUS[phase]
            if current(state.status_log, service) not in STATUS_AT_OR_AFTER[phase]:
                _error(session_id, f"Перед звонком нужен статус «{SERVICE_STATUS_LABELS[required]}» этой службы")
                return
            state.phone_pending = PhoneCallPending(service=service, crew=crew, phase=phase)
            state.dds_log.append(("phone.dial", now_utc(), crew))
            greeting = (f"{crew}, старший группы на связи. Назовите адрес, характер происшествия "
                        "и что требуется от бригады."
                        if phase == "dispatched" else
                        f"{crew}, старший группы на связи. Слушаю ваш запрос по карточке.")
            _line(session_id, state, "crew", greeting)
        case "phone.brief":
            call = state.phone_pending
            if call is None or call.phase != "dispatched":
                _error(session_id, "Сначала соединитесь со старшим группы для передачи вызова")
                return
            if not _address_matches(state.dispatched_card.address, event.address):
                _error(session_id, "Проверьте адрес: улица и номер дома должны совпадать с карточкой")
                return
            if not _incident_matches(state, event.incident):
                _error(session_id, "Уточните характер происшествия по данным карточки")
                return
            if not _has_purpose(event.request, ("выезд", "выех", "направ", "прибыт",
                                                "реагир", "помощ", "подтверд", "долож")):
                _error(session_id, "Сформулируйте задачу: выезд, помощь или доклад бригады")
                return
            _line(session_id, state, "dispatcher", f"Адрес: {event.address.strip()}. "
                  f"Происшествие: {event.incident.strip()}. {event.request.strip()}")
            _finish_phone_call(session_id, state)
        case "phone.check":
            call = state.phone_pending
            if call is None or call.phase == "dispatched":
                _error(session_id, "Сначала передайте вызов, затем запросите обстановку")
                return
            if not _has_purpose(event.text, ("обстанов", "статус", "прибыл", "доех",
                                              "выех", "работ", "заверш", "мест",
                                              "ход", "сообщ", "долож", "уточн")):
                _error(session_id, "Спросите обстановку, прибытие или ход работ по карточке")
                return
            _line(session_id, state, "dispatcher", event.text.strip())
            _finish_phone_call(session_id, state)
        case "phone.hangup":
            if state.phone_pending is None:
                _error(session_id, "Нет активного разговора")
                return
            state.dds_log.append(("phone.hangup", now_utc(), state.phone_pending.crew))
            state.phone_pending = None
        case "card.reply":
            if (state.exercise is not Exercise.DDS and not state.handoff_to_dds) or not state.dispatched_card or (
                event.card_id != state.dispatched_card.card_id
            ):
                _error(session_id, "Ответ относится не к текущей карточке")
                return
            state.reply_text = event.text
            state.reply_grammar = await assess(event.text)
            state.reply_log.append((now_utc(), event.text))
        case "card.open":
            if state.exercise is not Exercise.DDS or not state.activate_dds_card(event.card_id):
                _error(session_id, "Карточка отсутствует в текущей очереди")
                return
            hub.to_station(session_id, state.card_received_event())
            # CardReceived carries the contents, while StationState carries
            # the status journal and current queue. Send both on every switch
            # so the newly opened card cannot briefly inherit the previous
            # card's status snapshot until the next periodic tick.
            hub.to_station(session_id, StationState(snapshot=state.station_snapshot()))
        case "card.next":
            if state.exercise is not Exercise.DDS or not state.dispatched_card or (
                event.card_id != state.dispatched_card.card_id
            ):
                _error(session_id, "Следующая карточка недоступна: ID текущей не совпадает")
                return
            if any(item.card_id == event.card_id for item in state.dds_completed):
                _error(session_id, "Эта карточка уже завершена")
                return
            state.dds_completed.append(score_current_dds(state))
            completed_index = state.dds_card_index
            state.dds_live_cards = [
                item for item in state.dds_live_cards if item.card_id != event.card_id
            ]
            remaining = sorted(state.dds_live_cards, key=lambda item: item.original_index)
            if remaining:
                following = next(
                    (item for item in remaining if item.original_index > completed_index),
                    remaining[0],
                )
                state.activate_dds_card(following.card_id, capture=False)
                hub.to_station(session_id, state.card_received_event())
            else:
                # Keep the lesson alive if selected cards have not arrived yet.
                # The next delivery may become the active card immediately or
                # after its configured interval; no completed card is reused.
                state.dds_active_card_id = None
                state.dispatched_card = None
                state.dispatched_at = None
                state.dds_card_index = completed_index
            active_before_delivery = state.dds_active_card_id
            deliver_due_cards(state)
            if state.dds_active_card_id and state.dds_active_card_id != active_before_delivery:
                hub.to_station(session_id, state.card_received_event())
            hub.to_station(session_id, StationState(snapshot=state.station_snapshot()))
            if not state.dds_live_cards and len(state.dds_completed) >= len(state.dds_scenarios):
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
            state.on_event("zone.decision")
            state.dds_log.append(("zone.decision", now_utc(), "в зоне" if event.in_zone else "не в зоне"))
        case "crew.dispatched":
            state.kio = state.kio.model_copy(update={"dispatch_order_at": event.at})
            state.dds_log.append(("crew.dispatched", now_utc(), None))
        case "crew.arrived":
            state.on_event("crew.arrived")
            state.kio = state.kio.model_copy(update={"arrival_at": event.at})
            state.dds_log.append(("crew.arrived", now_utc(), None))
    state.capture_active_dds()
    await hub.checkpoint(session_id)
    hub.to_station(session_id, StationState(snapshot=state.station_snapshot()))
    hub.to_observers(session_id, state.snapshot())


async def _pump(ws: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        event = await queue.get()
        await ws.send_text(event.model_dump_json())


async def _reject(ws: WebSocket, message: str) -> None:
    """Отказ до входа в цикл: сокет закрывается с объяснением, а не молча."""
    await ws.send_text(
        ErrorEvent(code=ErrorKind.FORBIDDEN, message=message).model_dump_json()
    )
    await ws.close()


@router.websocket("/ws/station/{session_id}")
async def station(ws: WebSocket, session_id: UUID, role: str = "dds") -> None:
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
            if state.dispatched_card is not None:
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
                await _handle(session_id, state, event)
        except WebSocketDisconnect:
            return
        finally:
            sender.cancel()
