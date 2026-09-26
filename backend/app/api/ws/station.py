"""Сокет диспетчера ДДС.

Учебный исходящий звонок бригаде имитируется событиями JSON. Для целевого
локального прототипа не нужна внешняя телефония или аудиомодель.

Самая ценная механика цепочки — `card.bounce`: диспетчер видит, что не указан
этаж, и отбивает карточку обратно. Неполнота КИО перестаёт быть процентом
в отчёте и становится сорванным выездом с конкретной причиной.

Правила пульта — в `DdsDesk.apply`; здесь разбор команды, отсев повторов,
durable transition и рассылка итога.
"""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.api.auth import principal_of, websocket_origin_allowed
from app.domain.events import (
    CallEndReason,
    CommandAck,
    ErrorEvent,
    ErrorKind,
    ScoreReady,
    SessionEnded,
    StationState,
    StationToServer,
)
from app.domain.roles import Role
from app.session.finish import finish
from app.session.hub import LEASE_FENCED_MESSAGE, hub

log = logging.getLogger(__name__)
router = APIRouter()

_adapter = TypeAdapter(StationToServer)


def _error(session_id: UUID, message: str) -> None:
    hub.to_station(session_id, ErrorEvent(code=ErrorKind.UNSUPPORTED_EVENT, message=message))


async def _finish_dds(session_id: UUID, state) -> None:
    ended_at = state.end(CallEndReason.COMPLETE)
    hub.stop_ticker(session_id)
    hub.to_station(session_id, SessionEnded(reason=CallEndReason.COMPLETE))
    hub.to_observers(session_id, SessionEnded(reason=CallEndReason.COMPLETE))
    if hub.journal:
        await hub.journal.session_ended(session_id, ended_at, CallEndReason.COMPLETE.value)
    await finish(session_id, state)
    hub.to_station(session_id, ScoreReady(session_id=session_id))


async def _handle(session_id: UUID, state, event) -> None:
    outcome = state.desk.apply(event, state)
    if outcome.error is not None:
        _error(session_id, outcome.error)
        return
    for item in outcome.events:
        hub.to_station(session_id, item)
    if outcome.finished:
        await _finish_dds(session_id, state)
    if not outcome.changed:
        return
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
