"""Сокет курсанта-оператора 112.

Здесь только JSON-часть: приём вызова, правки карточки, подсказки, передача
в ДДС, завершение. Бинарные аудиокадры и голосовой контур — карточка lct-06.
"""

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.domain.events import (
    CallEnded,
    CallEndReason,
    CallStarted,
    ErrorEvent,
    ErrorKind,
    HintShown,
    KioState,
    SessionEnded,
    SessionMode,
    TimerTick,
    TraineeToServer,
)
from app.scenarios import store
from app.session.hub import hub
from app.session.state import now_utc

log = logging.getLogger(__name__)
router = APIRouter()

_adapter = TypeAdapter(TraineeToServer)


def _next_hint(state) -> tuple[str, str] | None:
    """Следующий пункт чек-листа, который ещё не подсказывали.

    Порядок временный: пока нет слот-автомата (lct-07), подсказка идёт
    по порядку чек-листа, а не по реально неотработанным пунктам.
    Перевод на слот-автомат — карточка lct-14.
    """
    scenario = store.get(state.scenario_id)
    if scenario is None:
        return None
    for item in scenario.checklist:
        if item.id not in state.hints_shown and item.question:
            return item.id, item.question
    return None


async def _handle(session_id: UUID, state, event) -> None:
    match event.type:
        case "call.answer":
            state.on_event("call.answer")
            state.started_at = now_utc()
            hub.to_trainee(session_id, CallStarted(started_at=state.started_at))
            hub.to_observers(session_id, state.snapshot())
            if hub.journal:
                await hub.journal.session_started(session_id, state.started_at)

        case "kio.patch":
            state.patch_kio(event.fields)
            # Наблюдателю уходит карточка целиком: рассинхрон на внешнем мониторе
            # посреди занятия дороже лишних килобайт.
            hub.to_observers(session_id, KioState(kio=state.kio))

        case "hint.request":
            if state.mode is SessionMode.EXAM:
                # На экзамене опоры нет — это часть нормы контроля.
                hub.to_trainee(
                    session_id,
                    ErrorEvent(
                        code=ErrorKind.HINT_DENIED_IN_EXAM,
                        message="В контрольном режиме подсказки недоступны",
                    ),
                )
                return
            nxt = _next_hint(state)
            if nxt is None:
                return
            checklist_id, question = nxt
            state.hints_shown.append(checklist_id)
            shown = HintShown(checklist_id=checklist_id, question=question)
            hub.broadcast(session_id, shown)
            if hub.journal:
                await hub.journal.hint(session_id, checklist_id, question, now_utc())

        case "dds.dispatch":
            state.on_event("dds.dispatch")
            state.dispatch(event.service.value)
            hub.to_observers(session_id, KioState(kio=state.kio))
            hub.to_observers(session_id, TimerTick(timers=state.timers.snapshot()))

        case "callback.dial":
            state.on_event("callback.dial")

        case "self_assessment.submit":
            if hub.journal:
                await hub.journal.self_assessment(
                    session_id, event.missed, event.comment, now_utc()
                )

        case "call.hangup":
            state.ended_at = now_utc()
            state.end_reason = CallEndReason.HANGUP
            hub.stop_ticker(session_id)
            hub.to_trainee(session_id, CallEnded(reason=CallEndReason.HANGUP))
            hub.to_observers(session_id, SessionEnded(reason=CallEndReason.HANGUP))
            if hub.journal:
                await hub.journal.session_ended(
                    session_id, state.ended_at, CallEndReason.HANGUP.value
                )


async def _pump(ws: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        event = await queue.get()
        await ws.send_text(event.model_dump_json())


@router.websocket("/ws/call/{session_id}")
async def call(ws: WebSocket, session_id: UUID) -> None:
    await ws.accept()

    state = hub.get(session_id)
    if state is None:
        await ws.send_text(
            ErrorEvent(
                code=ErrorKind.SESSION_NOT_FOUND, message="Занятие ещё не запущено преподавателем"
            ).model_dump_json()
        )
        await ws.close()
        return

    with hub.trainee(session_id) as queue:
        writer = asyncio.create_task(_pump(ws, queue))
        try:
            while True:
                payload = await ws.receive_json()
                try:
                    event = _adapter.validate_python(payload)
                except ValidationError:
                    hub.to_trainee(
                        session_id,
                        ErrorEvent(code=ErrorKind.UNSUPPORTED_EVENT, message=str(payload)[:200]),
                    )
                    continue
                await _handle(session_id, state, event)
        except WebSocketDisconnect:
            return
        finally:
            writer.cancel()
