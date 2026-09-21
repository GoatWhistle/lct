"""Сокет диспетчера ДДС.

Учебный исходящий звонок бригаде имитируется событиями JSON. Для целевого
локального прототипа не нужна внешняя телефония или аудиомодель.

Самая ценная механика цепочки — `card.bounce`: диспетчер видит, что не указан
этаж, и отбивает карточку обратно. Неполнота КИО перестаёт быть процентом
в отчёте и становится сорванным выездом с конкретной причиной.
"""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.domain.events import (
    CallEndReason, ErrorEvent, ErrorKind, Exercise, PhoneReport, ScoreReady,
    SessionEnded, StationState, StationToServer,
)
from app.domain.statuses import PRIMARY, PhoneReportRecord, ServiceStatus, StatusError, current
from app.api.auth import principal_of
from app.domain.roles import Role
from app.session.hub import hub
from app.session.state import now_utc
from app.session.finish import finish
from app.session.finish import score_current_dds
from app.session.dds import prepare_card

log = logging.getLogger(__name__)
router = APIRouter()

_adapter = TypeAdapter(StationToServer)

REPORT_PHASES = ("dispatched", "arrived", "working", "completed")
REPORT_TEXT = {
    "dispatched": "Бригада выехала к месту происшествия.",
    "arrived": "Бригада прибыла на место происшествия.",
    "working": "Бригада приступила к проведению работ.",
    "completed": "Работы завершены, бригада освобождена.",
}
REPORT_FOR_STATUS = {
    ServiceStatus.RESPONDING: "dispatched",
    ServiceStatus.ARRIVED: "arrived",
    ServiceStatus.WORKING: "working",
    ServiceStatus.COMPLETED: "completed",
}


def _error(session_id: UUID, message: str) -> None:
    hub.to_station(session_id, ErrorEvent(code=ErrorKind.UNSUPPORTED_EVENT, message=message))


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
            services = state.notified_services()
            if services:
                try:
                    state.set_service_status(services[0], ServiceStatus.ACCEPTED)
                except StatusError:
                    pass  # статус уже стоит: повторное нажатие ничего не меняет
        case "card.status":
            if event.service not in state.notified_services():
                _error(session_id, "Служба отсутствует в списке оповещения карточки")
                return
            phase = REPORT_FOR_STATUS.get(event.status)
            if (state.exercise is Exercise.DDS or state.handoff_to_dds) and phase is not None and not any(
                report.service == event.service and report.phase == phase
                for report in state.phone_reports
            ):
                _error(session_id, f"Статус «{event.status.value}» требует доклада бригады")
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
            state.crew_selected = event.crew
            state.crew_assignments[service] = event.crew
            state.dds_log.append(("crew.select", now_utc(), event.crew))
        case "phone.dial":
            crew = state.crew_selected
            service = state.crew_service(crew) if crew else None
            if service is None:
                _error(session_id, "Сначала выберите бригаду")
                return
            if current(state.status_log, service) not in {
                ServiceStatus.ACCEPTED, ServiceStatus.RESPONDING,
                ServiceStatus.ARRIVED, ServiceStatus.WORKING,
            }:
                _error(session_id, "Сначала примите карточку этой службы")
                return
            previous = [report for report in state.phone_reports if report.service == service]
            if len(previous) >= len(REPORT_PHASES):
                _error(session_id, "Все доклады этой бригады уже получены")
                return
            phase = REPORT_PHASES[len(previous)]
            report = PhoneReportRecord(
                service=service, crew=crew, phase=phase,
                text=REPORT_TEXT[phase], at=now_utc(),
            )
            state.phone_reports.append(report)
            state.dds_log.append(("phone.dial", now_utc(), crew))
            hub.to_station(session_id, PhoneReport(**report.model_dump()))
        case "card.reply":
            if (state.exercise is not Exercise.DDS and not state.handoff_to_dds) or not state.dispatched_card or (
                event.card_id != state.dispatched_card.card_id
            ):
                _error(session_id, "Ответ относится не к текущей карточке")
                return
            state.reply_text = event.text
            state.reply_log.append((now_utc(), event.text))
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
            if state.dds_card_index + 1 < len(state.dds_scenarios):
                state.dds_card_index += 1
                prepare_card(state, state.dds_scenarios[state.dds_card_index])
                hub.to_station(session_id, state.card_received_event())
            else:
                await _finish_dds(session_id, state)
        case "station.finish":
            if state.exercise is not Exercise.DDS and not state.handoff_to_dds:
                _error(session_id, "Операторское занятие завершается после звонка 112")
                return
            await _finish_dds(session_id, state)
        case "card.bounce":
            # Карточка вернулась: в разборе это E6 с конкретной причиной.
            state.bounced_fields = list(event.missing_fields)
            state.dds_log.append(("card.bounce", now_utc(), event.comment))
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
