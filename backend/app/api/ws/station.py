"""Сокет диспетчера ДДС.

**Только JSON: аудио здесь нет вообще** — значит, нет ни VAD, ни распознавания,
ни синтеза. Голосовой контур станция не трогает (docs/arch/CONTRACT.md).

Самая ценная механика цепочки — `card.bounce`: диспетчер видит, что не указан
этаж, и отбивает карточку обратно. Неполнота КИО перестаёт быть процентом
в отчёте и становится сорванным выездом с конкретной причиной.
"""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.domain.events import ErrorEvent, ErrorKind, StationToServer
from app.session.hub import hub
from app.session.state import now_utc

log = logging.getLogger(__name__)
router = APIRouter()

_adapter = TypeAdapter(StationToServer)


async def _handle(session_id: UUID, state, event) -> None:
    match event.type:
        case "card.ack":
            state.on_event("card.ack")
            state.dds_log.append(("card.ack", now_utc(), None))
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
    hub.to_observers(session_id, state.snapshot())


async def _pump(ws: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        event = await queue.get()
        await ws.send_text(event.model_dump_json())


@router.websocket("/ws/station/{session_id}")
async def station(ws: WebSocket, session_id: UUID, role: str = "dds") -> None:
    await ws.accept()

    state = hub.get(session_id)
    if state is None:
        await ws.send_text(
            ErrorEvent(code=ErrorKind.SESSION_NOT_FOUND, message="Занятие не запущено").model_dump_json()
        )
        await ws.close()
        return

    with hub.station(session_id) as queue:
        sender = asyncio.create_task(_pump(ws, queue))
        try:
            # Карточка, переданная до подключения станции, не теряется:
            # диспетчер садится за АРМ, когда вызов уже идёт.
            if state.dispatched_card is not None:
                hub.to_station(session_id, state.card_received_event())
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
