"""Сокет диспетчера ДДС.

Учебный исходящий звонок бригаде имитируется событиями JSON. Для целевого
локального прототипа не нужна внешняя телефония или аудиомодель.

`card.bounce` сервер отклоняет: ДДС не возвращает карточку в 112 при принятии
и не правит её поля. Об ошибке, которую вскрыл доклад бригады с места,
диспетчер сообщает в 112 по телефону (ответ заказчика, П.3).

Правила пульта — в `DdsDesk.apply`; здесь разбор команды и рассылка итога.
Отсев повторов и операция хранилища — в `run_command`.
"""

import asyncio
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.api.ws.session import close_fenced, pump, run_command, session_socket
from app.domain.events import (
    CallEndReason,
    ErrorEvent,
    ErrorKind,
    StationState,
    StationToServer,
)
from app.domain.roles import Role
from app.session.finish import end_session
from app.session.hub import hub

router = APIRouter()

_adapter = TypeAdapter(StationToServer)


def _error(session_id: UUID, message: str) -> None:
    hub.to_station(session_id, ErrorEvent(code=ErrorKind.UNSUPPORTED_EVENT, message=message))


async def _handle(session_id: UUID, state, event) -> None:
    outcome = state.desk.apply(event, state)
    if outcome.error is not None:
        _error(session_id, outcome.error)
        return
    for item in outcome.events:
        hub.to_station(session_id, item)
    if outcome.finished:
        await end_session(session_id, state, CallEndReason.COMPLETE)
    if not outcome.changed:
        return
    hub.to_station(session_id, StationState(snapshot=state.station_snapshot()))
    hub.to_observers(session_id, state.snapshot())


@router.websocket("/ws/station/{session_id}")
async def station(ws: WebSocket, session_id: UUID, role: str = "dds") -> None:
    # За АРМ ДДС садится обучающийся, преподаватель смотрит и подменяет.
    entered = await session_socket(ws, session_id, (Role.INSTRUCTOR, Role.TRAINEE))
    if entered is None:
        return
    who, state = entered

    async with hub.trainee_socket(
        session_id, station=True, trainee=who.role is Role.TRAINEE,
    ) as queue:
        if hub.is_lease_fenced(session_id):
            await close_fenced(ws)
            return
        sender = asyncio.create_task(pump(ws, queue))
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
                if not await run_command(
                    ws, session_id, lambda: _handle(session_id, state, event),
                    command_id=command_id, sender=sender,
                    ack=lambda item: hub.to_station(session_id, item),
                ):
                    return
        except WebSocketDisconnect:
            return
        finally:
            sender.cancel()
