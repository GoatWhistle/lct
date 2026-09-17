"""Сокет курсанта-оператора 112.

Здесь только JSON-часть: приём вызова, правки карточки, подсказки, передача
в ДДС, завершение. Бинарные аудиокадры и голосовой контур — карточка lct-06.
"""

import asyncio
import json
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
from app.domain.events import BgStart
from app.scenarios import store
from app.session.finish import finish, release_score
from app.session.hub import hub
from app.session.state import now_utc
from app.voice.models import get_voice_models
from app.voice.pipeline import VoiceSession

log = logging.getLogger(__name__)
router = APIRouter()

#: Кадр контракта: 20 мс PCM16 моно 16 кГц = 320 сэмплов = 640 байт.
FRAME_BYTES = 640
FRAMES_PER_LOG = 250  # раз в пять секунд звука

_adapter = TypeAdapter(TraineeToServer)


def _on_audio(session_id: UUID, state, frame: bytes) -> None:
    """Приём аудиокадра: в голосовой контур, а без него — только счёт."""
    if len(frame) != FRAME_BYTES:
        state.bad_frames += 1
        if state.bad_frames == 1:
            # Один раз, а не на каждый кадр: неверный формат повторяется 50 раз в секунду.
            log.warning("сессия %s: кадр %d байт вместо %d — проверь ресемплинг на фронте",
                        session_id, len(frame), FRAME_BYTES)
        return
    state.audio_frames += 1
    if state.voice is not None:
        state.voice.feed(frame)
    if state.audio_frames % FRAMES_PER_LOG == 0:
        log.info("сессия %s: получено %d кадров (%.0f с звука)",
                 session_id, state.audio_frames, state.audio_frames * 0.02)


def _next_hint(state) -> tuple[str, str] | None:
    """Следующий неотработанный пункт чек-листа, который ещё не подсказывали.

    «Неотработанный» знает слот-автомат: пункт, о котором оператор уже спросил
    своими словами, подсказывать бессмысленно. Без модели эмбеддингов автомата
    нет — тогда подсказка идёт по порядку чек-листа.
    """
    if state.slots is not None:
        candidates = state.slots.unasked()
    else:
        scenario = state.scenario or store.get(state.scenario_id)
        candidates = scenario.checklist if scenario else []
    for item in candidates:
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
            _start_voice(session_id, state)

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
            state.hints_log.append((checklist_id, now_utc()))
            shown = HintShown(checklist_id=checklist_id, question=question)
            hub.broadcast(session_id, shown)
            if hub.journal:
                await hub.journal.hint(session_id, checklist_id, question, now_utc())

        case "dds.dispatch":
            state.on_event("dds.dispatch")
            state.dispatch(event.service.value)
            # Карточка замораживается снимком и уходит диспетчеру: оператор
            # не должен иметь возможности дописать поле задним числом.
            hub.to_station(session_id, state.card_received_event())
            hub.to_observers(session_id, KioState(kio=state.kio))
            hub.to_observers(session_id, TimerTick(timers=state.timers.snapshot()))

        case "callback.dial":
            state.on_event("callback.dial")

        case "self_assessment.submit":
            state.self_assessed = True
            state.self_assessment = {"missed": event.missed, "comment": event.comment}
            if hub.journal:
                await hub.journal.self_assessment(
                    session_id, event.missed, event.comment, now_utc()
                )
            # Оценка могла быть готова раньше самооценки — теперь её можно отдать.
            await release_score(session_id, state)

        case "call.hangup":
            if state.voice is not None:
                await state.voice.close()
            state.ended_at = now_utc()
            state.end_reason = CallEndReason.HANGUP
            hub.stop_ticker(session_id)
            hub.to_trainee(session_id, CallEnded(reason=CallEndReason.HANGUP))
            hub.to_observers(session_id, SessionEnded(reason=CallEndReason.HANGUP))
            if hub.journal:
                await hub.journal.session_ended(
                    session_id, state.ended_at, CallEndReason.HANGUP.value
                )
            await finish(session_id, state)


def _start_voice(session_id: UUID, state) -> None:
    """Голос включается, когда курсант снял трубку: звонящий сразу кричит первую реплику."""
    models = get_voice_models()
    scenario = store.get(state.scenario_id)
    if models is None or scenario is None or state.voice is not None:
        return
    state.voice = VoiceSession(
        session_id=session_id,
        state=state,
        models=models,
        send_event=lambda event: hub.to_trainee(session_id, event),
        send_observer=lambda event: hub.to_observers(session_id, event),
        send_audio=lambda pcm: hub.to_trainee(session_id, pcm),
        journal=hub.journal,
    )
    if scenario.background:
        event = BgStart(loop=scenario.background.loop, gain_db=scenario.background.gain_db)
        hub.broadcast(session_id, event)
    state.voice.speak(scenario.first_line, state.persona.mood)


async def _pump(ws: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        item = await queue.get()
        # Бинарь — звук звонящего, без обёртки JSON (docs/arch/CONTRACT.md).
        if isinstance(item, bytes):
            await ws.send_bytes(item)
        else:
            await ws.send_text(item.model_dump_json())


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
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                # Бинарные кадры — аудио, текстовые — события. Направление определяется
                # каналом, обёртки JSON вокруг звука нет (docs/arch/CONTRACT.md).
                if message.get("bytes") is not None:
                    _on_audio(session_id, state, message["bytes"])
                    continue
                try:
                    payload = json.loads(message.get("text") or "")
                except json.JSONDecodeError:
                    hub.to_trainee(
                        session_id,
                        ErrorEvent(code=ErrorKind.UNSUPPORTED_EVENT, message="не JSON"),
                    )
                    continue
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
