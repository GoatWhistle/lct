"""Общее для сокетов занятия: вход, доставка очереди хаба и выполнение команды.

Лестница входа одна для всех каналов, чтобы коды закрытия и тексты отказов
не расходились: 1008 — чужой origin, 1012 — занятие ушло другому узлу ещё
до accept, `ErrorEvent` и close — нет прав, нет занятия, не назначено.

Команда выполняется одной операцией хаба: сверка `(node_id, epoch)` остаётся
в её commit перед каждой мутацией, здесь только место вызова.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection
from uuid import UUID

from fastapi import WebSocket
from pydantic import BaseModel

from app.api.auth import Principal, principal_of, websocket_origin_allowed
from app.domain.events import CommandAck, ErrorEvent, ErrorKind
from app.domain.roles import Role
from app.session.access import can_access
from app.session.hub import LEASE_FENCED_MESSAGE, hub
from app.session.state import MAX_STATION_COMMANDS, SessionState

log = logging.getLogger(__name__)

NOT_STARTED = "Занятие не запущено"
FORBIDDEN = "Недостаточно прав для этого экрана"
NOT_ASSIGNED = "Занятие не назначено этому обучающемуся"

#: Сколько ждать, пока `pump` доставит событие fencing сам. Очередь могла
#: переполниться и выпасть из подписки — тогда сокет закрывается здесь.
_FENCE_DELIVERY_SECONDS = 2.0


def _fenced_event() -> ErrorEvent:
    return ErrorEvent(code=ErrorKind.INTERNAL, message=LEASE_FENCED_MESSAGE)


def _is_fenced_event(item) -> bool:
    return (isinstance(item, ErrorEvent) and item.code is ErrorKind.INTERNAL
            and item.message == LEASE_FENCED_MESSAGE)


async def _refuse(ws: WebSocket, code: ErrorKind, message: str) -> None:
    """Отказ до входа в цикл: сокет закрывается с объяснением, а не молча."""
    await ws.send_text(ErrorEvent(code=code, message=message).model_dump_json())
    await ws.close()


async def session_socket(
    ws: WebSocket,
    session_id: UUID,
    roles: Collection[Role],
    *,
    require_state: bool = True,
    not_found: str = NOT_STARTED,
) -> tuple[Principal, SessionState | None] | None:
    """Принципал и живое занятие — или `None`, если сокет уже закрыт.

    `require_state=False` — для пульта: занятие создаёт его же `scenario.start`,
    поэтому вход занятие не смотрит, а доступ проверяется на каждой команде.
    """
    if not websocket_origin_allowed(ws):
        await ws.close(code=1008)
        return None
    if hub.is_lease_fenced(session_id):
        await ws.close(code=1012)
        return None
    await ws.accept()

    who = principal_of(ws)
    if who is None or who.role not in roles:
        await _refuse(ws, ErrorKind.FORBIDDEN, FORBIDDEN)
        return None
    if not require_state:
        return who, None
    state = hub.get(session_id)
    if state is None:
        await _refuse(ws, ErrorKind.SESSION_NOT_FOUND, not_found)
        return None
    if not can_access(who, state):
        # Чужому преподавателю занятие «не существует»: отказ не выдаёт владельца.
        if who.role is Role.TRAINEE:
            await _refuse(ws, ErrorKind.FORBIDDEN, NOT_ASSIGNED)
        else:
            await _refuse(ws, ErrorKind.SESSION_NOT_FOUND, not_found)
        return None
    return who, state


async def pump(ws: WebSocket, queue: asyncio.Queue) -> None:
    """Очередь хаба в сокет; событие fencing закрывает его с 1012."""
    while True:
        item = await queue.get()
        # Бинарь — звук звонящего, без обёртки JSON (docs/arch/CONTRACT.md).
        if isinstance(item, bytes):
            await ws.send_bytes(item)
            continue
        await ws.send_text(item.model_dump_json())
        if _is_fenced_event(item):
            await ws.close(code=1012)
            return


async def close_fenced(ws: WebSocket, sender: asyncio.Task | None = None) -> None:
    """Структурное событие fencing и 1012 вместо безымянного 1006.

    С очередью событие уже разослал `hub.fence` — его доставляет `pump`.
    Без очереди (пульт) сообщение уходит прямо в сокет.
    """
    if sender is not None:
        done, _ = await asyncio.wait({sender}, timeout=_FENCE_DELIVERY_SECONDS)
        if done:
            return
        sender.cancel()
    await ws.send_text(_fenced_event().model_dump_json())
    await ws.close(code=1012)


async def run_command(
    ws: WebSocket,
    session_id: UUID,
    handler: Callable[[], Awaitable[None]],
    *,
    command_id: UUID | None = None,
    ack: Callable[[BaseModel], None] | None = None,
    sender: asyncio.Task | None = None,
) -> bool:
    """Одна команда — одна операция хаба. `False` — сокет закрыт fencing.

    С `command_id` повтор уже зафиксированной команды только подтверждается:
    id входит в тот же снимок, что и переход, а `CommandAck` копится в
    операции и уходит после её commit.
    """
    state = hub.get(session_id)
    if command_id is not None and state is not None:
        if str(command_id) in state.processed_station_commands:
            if ack is not None:
                ack(CommandAck(command_id=command_id))
            return True
    try:
        async with hub.operation(session_id):
            await handler()
            if command_id is not None and state is not None:
                state.processed_station_commands.append(str(command_id))
                del state.processed_station_commands[:-MAX_STATION_COMMANDS]
                if ack is not None:
                    ack(CommandAck(command_id=command_id))
    except Exception:
        if not hub.is_lease_fenced(session_id):
            raise
        log.info("закрытие WebSocket после fencing занятия %s", session_id)
        await close_fenced(ws, sender)
        return False
    if hub.is_lease_fenced(session_id):
        await close_fenced(ws, sender)
        return False
    return True
