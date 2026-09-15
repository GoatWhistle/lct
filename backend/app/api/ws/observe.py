"""Канал наблюдателя: внешний монитор и пульт преподавателя.

**На этом сокете нет ни одного обработчика входящих сообщений.** Внешний
монитор физически не может повлиять на занятие: у него нет ни аудио, ни канала
записи. Преподаватель смотрит здесь, а пишет через отдельный `control`.

Входящие кадры читаются и выбрасываются, не разбираясь: чтение нужно ровно
затем, чтобы заметить разрыв соединения. Без него задача сокета висела бы
на очереди до первой неудачной отправки, а закрытая вкладка монитора оставляла
бы за собой подписку. Разбора, диспетчеризации и эффекта у входящих нет.
"""

import asyncio
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.domain.events import ErrorEvent, ErrorKind
from app.session.hub import hub

router = APIRouter()


async def _pump(ws: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        event = await queue.get()
        await ws.send_text(event.model_dump_json())


async def _wait_for_disconnect(ws: WebSocket) -> None:
    """Единственное назначение — дождаться разрыва. Содержимое кадров
    не читается и никуда не передаётся."""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return


@router.websocket("/ws/observe/{session_id}")
async def observe(ws: WebSocket, session_id: UUID) -> None:
    await ws.accept()

    state = hub.get(session_id)
    if state is None:
        await ws.send_text(
            ErrorEvent(code=ErrorKind.SESSION_NOT_FOUND, message="Занятие не запущено").model_dump_json()
        )
        await ws.close()
        return

    # Снимок при подключении обязателен: монитор в классе включают посреди
    # занятия, и он должен показать текущее состояние, а не ждать событий.
    await ws.send_text(state.snapshot().model_dump_json())

    with hub.observer(session_id) as queue:
        sender = asyncio.create_task(_pump(ws, queue))
        try:
            await _wait_for_disconnect(ws)
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
