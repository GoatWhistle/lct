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
    CallEnded,
    ErrorEvent,
    ErrorKind,
    ScoreReady,
    CallIncoming,
    ErrorEvent,
    ErrorKind,
    InstructorToServer,
    InstructorNoteShown,
    ModeSet,
    ReferenceStarted,
    SessionEnded,
)
import asyncio

from app.dialog.factory import build_caller
from app.dialog.director import apply as apply_directive
from app.dialog.director import mood_of
from app.dialog.persona import PersonaState
from app.dialog.runtime import get_embedder
from app.dialog.slots import SlotMachine
from app.scenarios import store
from app.session.hub import hub
from app.session.state import SessionState, now_utc
from app.voice.models import get_voice_models
from app.voice.pipeline import FILLERS, prefetch

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

    # Занятие собирается целиком и только потом регистрируется: иначе
    # наблюдатель, подключившийся в эту щель, увидит полусобранное состояние
    # без слот-автомата и звонящего.
    state = SessionState(
        session_id=session_id,
        scenario_id=scenario.id,
        scenario_title=scenario.title,
        level=scenario.level.value,
        mode=event.mode,
        scenario=scenario.model_copy(deep=True),
        required_fields=scenario.required_fields,
        trainee_name=event.trainee,
        attempt=attempt,
    )
    embedder = get_embedder()
    if embedder is not None:
        state.slots = SlotMachine(state.scenario, embedder)
    state.persona = PersonaState(state.scenario.persona)
    state.caller = build_caller(scenario.id)
    hub.register(state)

    # Первая реплика и филлеры синтезируются, пока курсант не снял трубку:
    # «Алло! Помогите!» должно прозвучать мгновенно (docs/arch/BACKEND.md).
    models = get_voice_models()
    if models is not None:
        asyncio.create_task(prefetch(models, [scenario.first_line, *FILLERS.values()]))
    state.on_event("call.incoming")
    hub.start_ticker(session_id)

    hub.to_trainee(
        session_id,
        CallIncoming(
            scenario_id=scenario.id,
            caller_number="+7 (495) 000-00-00",
            level=scenario.level,
            mode=event.mode,
            scenario=scenario.model_copy(deep=True),
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
                    state = hub.get(session_id)
                    if state is not None:
                        state.notes.append({
                            "type": "instructor_note.shown",
                            "transcript_ref": event.transcript_ref,
                            "text": event.text,
                            "author": "преподаватель",
                        })
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
                case "score.override":
                    state = hub.get(session_id)
                    if state is not None and state.score is not None:
                        # Автооценка остаётся рядом: видно, что скорректировано и кем.
                        state.score = {
                            **state.score,
                            "score_final": float(event.verdict) if event.verdict.replace(".", "", 1).isdigit() else state.score["score_auto"],
                            "overridden_by": "преподаватель",
                            "override_comment": event.comment,
                        }
                        hub.to_observers(session_id, ScoreReady(session_id=session_id))
                case "director.inject":
                    state = hub.get(session_id)
                    if state is None:
                        continue
                    result = apply_directive(state, event.directive)
                    if result.needs_network:
                        hub.to_observers(session_id, ErrorEvent(
                            code=ErrorKind.DIRECTIVE_NEEDS_NETWORK,
                            message="Свободный текст требует LLM: офлайн доступны только кнопки",
                        ))
                        continue
                    state.directives.append(event.directive)
                    voice = state.voice
                    if result.drop_line and voice is not None:
                        # Обрыв рвёт звук на полуслове тем же механизмом, что
                        # перебивание, и запускает норматив обратного дозвона.
                        voice.barge_in()
                    if result.drop_line:
                        state.on_event("call.dropped")
                        hub.to_trainee(session_id, CallEnded(reason=CallEndReason.DROPPED))
                        hub.to_observers(session_id, SessionEnded(reason=CallEndReason.DROPPED))
                    elif result.say and voice is not None:
                        voice.speak(result.say, mood_of(state))
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
