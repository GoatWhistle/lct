"""Канал преподавателя: только передача.

**Ни одной команды, меняющей карточку курсанта.** Преподаватель управляет
ситуацией, а не работой обучаемого, иначе оценка перестаёт быть оценкой
курсанта (docs/arch/CONTRACT.md).

Ответы сюда не идут — канал односторонний. Всё, что сервер хочет сказать
преподавателю, уходит на его же сокет `observe`.
"""

import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.domain.events import (
    CallEndReason,
    CallIncoming,
    ErrorEvent,
    ErrorKind,
    InstructorToServer,
    InstructorNoteShown,
    ModeSet,
    ReferenceStarted,
    SessionEnded,
)
from app.scenarios import store
from app.session.hub import hub
from app.session.state import SessionState, now_utc

log = logging.getLogger(__name__)
router = APIRouter()

_adapter = TypeAdapter(InstructorToServer)


async def _start(session_id: UUID, event) -> None:
    scenario = store.get(event.scenario_id)
    if scenario is None:
        hub.to_observers(
            session_id,
            ErrorEvent(code=ErrorKind.SCENARIO_INVALID, message=f"Нет сценария {event.scenario_id}"),
        )
        return

    attempt = 1
    if hub.journal:
        attempt = await hub.journal.start_lesson(
            session_id, scenario.id, event.mode.value, event.trainee
        )

    state = hub.register(
        SessionState(
            session_id=session_id,
            scenario_id=scenario.id,
            scenario_title=scenario.title,
            level=scenario.level.value,
            mode=event.mode,
            required_fields=scenario.required_fields,
            trainee_name=event.trainee,
            attempt=attempt,
        )
    )
    state.on_event("call.incoming")
    hub.start_ticker(session_id)

    hub.to_trainee(
        session_id,
        CallIncoming(
            scenario_id=scenario.id,
            caller_number="+7 (495) 000-00-00",
            level=scenario.level,
            mode=event.mode,
            required_fields=scenario.required_fields,
        ),
    )
    hub.to_observers(session_id, ModeSet(mode=event.mode))
    hub.to_observers(session_id, state.snapshot())


async def _stop(session_id: UUID) -> None:
    state = hub.get(session_id)
    if state is None:
        return
    state.ended_at = now_utc()
    state.end_reason = CallEndReason.INSTRUCTOR
    hub.stop_ticker(session_id)
    hub.to_observers(session_id, SessionEnded(reason=CallEndReason.INSTRUCTOR))
    if hub.journal:
        await hub.journal.session_ended(session_id, state.ended_at, CallEndReason.INSTRUCTOR.value)


@router.websocket("/ws/control/{session_id}")
async def control(ws: WebSocket, session_id: UUID) -> None:
    await ws.accept()
    try:
        while True:
            payload = await ws.receive_json()
            try:
                event = _adapter.validate_python(payload)
            except ValidationError:
                hub.to_observers(
                    session_id,
                    ErrorEvent(code=ErrorKind.UNSUPPORTED_EVENT, message=str(payload)[:200]),
                )
                continue

            match event.type:
                case "scenario.start":
                    await _start(session_id, event)
                case "session.stop":
                    await _stop(session_id)
                case "instructor_note.add":
                    hub.to_observers(
                        session_id,
                        InstructorNoteShown(
                            transcript_ref=event.transcript_ref,
                            text=event.text,
                            author="преподаватель",
                        ),
                    )
                    if hub.journal:
                        await hub.journal.note(
                            session_id, event.transcript_ref, event.text, "преподаватель"
                        )
                case "reference.play":
                    state = hub.get(session_id)
                    if state is not None:
                        hub.to_observers(session_id, ReferenceStarted(scenario_id=state.scenario_id))
                case "director.inject":
                    # Поведение звонящего — карточка lct-07, пульт — lct-22.
                    # До них директива копится в состоянии и видна в разборе.
                    state = hub.get(session_id)
                    if state is not None:
                        state.directives.append(event.directive)
                case _:
                    hub.to_observers(
                        session_id,
                        ErrorEvent(
                            code=ErrorKind.UNSUPPORTED_EVENT,
                            message=f"{event.type} ещё не реализовано",
                        ),
                    )
    except WebSocketDisconnect:
        return
