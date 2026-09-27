"""Сокет курсанта-оператора 112.

Здесь только JSON-часть: приём вызова, правки карточки, подсказки, передача
в ДДС, завершение. Бинарные аудиокадры и голосовой контур — карточка lct-06.
"""

import asyncio
import json
import logging
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.api.ws.session import pump, run_command, session_socket
from app.domain.events import (
    BgStart,
    CallEnded,
    CallEndReason,
    CallIncoming,
    CallStarted,
    CallerUtterance,
    ErrorEvent,
    ErrorKind,
    Exercise,
    HintShown,
    KioPatchOut,
    KioState,
    PatchSource,
    ScoreReady,
    SessionMode,
    StationState,
    TimerTick,
    TextTurnAccepted,
    Speaker,
    TranscriptAppend,
    TraineeToServer,
)
from app.dialog.slots import TurnResult
from app.domain.roles import Role
from app.scenarios import store
from app.session.dds import prepare_handoff_queue
from app.session.finish import end_session, refresh_archived_report, release_score
from app.session.hub import hub
from app.session.state import now_utc
from app.session.store import (
    HintRecorded,
    LessonStarted,
    SelfAssessed,
    UtteranceAppended,
)
from app.voice.models import TTS_RATE, get_voice_models
from app.voice.pipeline import VoiceSession
from app.voice.recording import start_recording

log = logging.getLogger(__name__)
router = APIRouter()

#: Кадр контракта: 20 мс PCM16 моно 16 кГц = 320 сэмплов = 640 байт.
FRAME_BYTES = 640
FRAMES_PER_LOG = 250  # раз в пять секунд звука

_adapter = TypeAdapter(TraineeToServer)


class _TextSlotView:
    """Grounded facts for the text exercise when the optional embedder is absent."""
    def __init__(self, state):
        self.scenario = state.scenario
        self.state = state

    def revealed_facts(self):
        return [SimpleNamespace(id=fact.id, value=self.state.text_revealed_facts[fact.id])
                for fact in self.scenario.facts if fact.id in self.state.text_revealed_facts]


def _text_turn(state, text: str):
    """Match typed questions to approved checklist prompts; never let the model
    decide which hidden scenario fact becomes available."""
    turn = None
    if state.slots is not None:
        turn = state.slots.hear(text)
        for fact in state.slots.revealed_facts():
            state.text_revealed_facts[fact.id] = fact.value
        if turn.refined:
            return turn

    # The lexical offline matcher misses natural follow-ups such as “а точнее,
    # ближайший дом?”. Once the caller has disclosed a fact with a refinement,
    # allow an explicit request for precision to reveal only that refined value.
    # This remains a deterministic slot rule: the model never chooses the fact.
    normalized = text.casefold().replace("ё", "е")
    asks_for_precision = bool(re.search(
        r"\b(точн\w*|конкретн\w*|ближ\w*|номер\w*|уточн\w*)\b", normalized
    ))
    if asks_for_precision:
        for fact in state.scenario.facts:
            if (fact.id in state.text_revealed_facts and fact.refine_on and fact.refined):
                state.text_revealed_facts[fact.id] = fact.refined
                if state.slots is not None:
                    if fact.id not in state.slots.refined:
                        state.slots.refined.append(fact.id)
                    if fact.id not in state.slots.revealed:
                        state.slots.revealed.append(fact.id)
                    if fact.refine_on not in state.slots.asked:
                        state.slots.asked.append(fact.refine_on)
                return TurnResult(text=text, matched=[fact.refine_on], refined=[fact.id])

    if turn is not None and turn.matched:
        return turn

    words = set(re.findall(r"[а-яё]{3,}", text.casefold().replace("ё", "е")))
    stop = {"что", "как", "где", "когда", "сколько", "есть", "это", "или", "вас", "вам", "пожалуйста"}
    words -= stop
    best = None
    best_score = 0.0
    for item in state.scenario.checklist:
        if not item.question:
            continue
        for phrase in [item.question, *item.examples]:
            prompt_words = set(re.findall(r"[а-яё]{3,}", phrase.casefold().replace("ё", "е"))) - stop
            score = len(words & prompt_words) / max(1, len(prompt_words))
            if score > best_score:
                best, best_score = item, score
    turn = TurnResult(text=text)
    if best is None or best_score < 0.25:
        return turn
    turn.matched.append(best.id)
    fact_ids = [fact.id for fact in state.scenario.facts
                if fact.reveal_on and fact.reveal_on.question == best.id]
    if best.fact and best.fact not in fact_ids:
        fact_ids.append(best.fact)
    for fact in state.scenario.facts:
        if fact.refine_on == best.id and fact.refined:
            state.text_revealed_facts[fact.id] = fact.refined
            turn.refined.append(fact.id)
    for fact_id in fact_ids:
        fact = next((item for item in state.scenario.facts if item.id == fact_id), None)
        if fact is None:
            continue
        if fact_id in state.text_revealed_facts:
            turn.repeated.append(fact_id)
        else:
            state.text_revealed_facts[fact_id] = fact.value
            turn.revealed.append(fact_id)
    return turn


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
    if state.recorder is not None:
        state.recorder.add_pcm(frame, sample_rate=16_000)
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
    if state.ended and event.type != "self_assessment.submit":
        hub.to_trainee(session_id, ErrorEvent(
            code=ErrorKind.UNSUPPORTED_EVENT, message="Занятие уже завершено",
        ))
        return
    if (event.type == "text.turn" and state.exercise is not Exercise.CARD) or state.exercise is Exercise.DDS or (
        state.exercise is Exercise.CARD and event.type not in {"kio.patch", "card.submit", "text.turn"}
    ) or (state.exercise is Exercise.CALL and event.type == "card.submit"):
        hub.to_trainee(session_id, ErrorEvent(
            code=ErrorKind.UNSUPPORTED_EVENT, message="Действие недоступно в этом упражнении",
        ))
        return
    if state.exercise is Exercise.CARD and state.dispatched_card is not None:
        hub.to_trainee(session_id, ErrorEvent(
            code=ErrorKind.UNSUPPORTED_EVENT,
            message="Карточка уже передана в ДДС и не может быть изменена",
        ))
        return
    match event.type:
        case "text.turn":
            if state.caller is None or state.persona is None or state.scenario is None:
                hub.to_trainee(session_id, ErrorEvent(
                    code=ErrorKind.MODELS_WARMING_UP,
                    message="Текстовый диалог пока не готов. Обновите занятие или заполните карточку по вводной.",
                ))
                return
            turn = _text_turn(state, event.text)
            operator_entry = state.append(Speaker.OPERATOR, event.text)
            accepted = TextTurnAccepted(text=event.text, at=operator_entry.at)
            hub.to_trainee(session_id, accepted)
            hub.to_observers(session_id, TranscriptAppend(entry=operator_entry))
            hub.record(session_id, UtteranceAppended(operator_entry))
            try:
                slots = state.slots if state.slots is not None else _TextSlotView(state)
                line = await state.caller.reply(turn, state.persona, slots)
            except Exception as exc:  # noqa: BLE001
                # The model/provider exception can contain the prompt and incident facts.
                log.error("text dialogue failed for session %s (%s)",
                          session_id, type(exc).__name__)
                hub.to_trainee(session_id, ErrorEvent(
                    code=ErrorKind.INTERNAL, message="Не удалось получить ответ заявителя. Попробуйте ещё раз.",
                ))
                return
            caller_entry = state.append(Speaker.CALLER, line.text, line.mood)
            hub.to_trainee(session_id, CallerUtterance(
                utterance_id=uuid4(), text=line.text,
                at=caller_entry.at, mood=line.mood, source=line.source,
            ))
            hub.to_observers(session_id, TranscriptAppend(entry=caller_entry))
            hub.record(session_id, UtteranceAppended(caller_entry))
        case "card.submit":
            state.on_event("card.submit")
            state.kio.registered_at = state.started_at or now_utc()
            state.dispatch()
            if state.handoff_to_dds:
                prepare_handoff_queue(
                    state,
                    state.pending_dds_scenarios,
                    arrival_interval_seconds=state.desk.arrival_interval_seconds,
                    max_waiting=state.desk.max_waiting,
                )
                state.pending_dds_scenarios = []
            hub.to_station(session_id, state.card_received_event())
            hub.to_station(session_id, StationState(snapshot=state.station_snapshot()))
            hub.to_observers(session_id, KioState(kio=state.kio))
            if state.handoff_to_dds:
                hub.to_trainee(session_id, CallEnded(reason=CallEndReason.COMPLETE))
                hub.to_observers(session_id, state.snapshot())
            else:
                await end_session(session_id, state, CallEndReason.COMPLETE)
        case "call.answer":
            first_answer = state.started_at is None
            if first_answer:
                state.on_event("call.answer")
                state.started_at = now_utc()
                hub.record(session_id, LessonStarted(state.started_at))
            hub.to_trainee(session_id, CallStarted(started_at=state.started_at))
            hub.to_observers(session_id, state.snapshot())
            if state.recorder is None:
                state.recorder = start_recording(session_id)
            _start_voice(session_id, state, initial_statement=first_answer)

        case "kio.patch":
            old_code, old_notify = state.kio.incident_code, list(state.kio.notify)
            state.patch_kio(event.fields)
            hub.to_trainee(session_id, KioPatchOut(
                fields=event.fields, source=PatchSource.OPERATOR,
            ))
            if (state.kio.incident_code, state.kio.notify) != (old_code, old_notify):
                hub.to_trainee(session_id, KioPatchOut(
                    fields={"incident_code": state.kio.incident_code,
                            "notify": list(state.kio.notify)},
                    source=PatchSource.AUTO,
                ))
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
            hub.record(session_id, HintRecorded(checklist_id, question, now_utc()))

        case "dds.dispatch":
            if event.service is None and not (state.kio.incident_code or state.kio.notify):
                hub.to_trainee(session_id, ErrorEvent(
                    code=ErrorKind.UNSUPPORTED_EVENT,
                    message="Укажите ДДС или проставьте признаки для маршрутизации по ЕКП",
                ))
                return
            state.on_event("dds.dispatch")
            state.dispatch(event.service.value if event.service else None)
            hub.to_trainee(session_id, KioPatchOut(
                fields={"response_status": state.kio.response_status.value,
                        "dds": state.kio.dds.value if state.kio.dds else None},
                source=PatchSource.AUTO,
            ))
            # Карточка замораживается снимком и уходит диспетчеру: оператор
            # не должен иметь возможности дописать поле задним числом.
            hub.to_station(session_id, state.card_received_event())
            # Список оповещения диспетчер должен видеть сразу, а не после
            # первого своего действия: отмечаться ему по нему же (lct-33).
            hub.to_station(session_id, StationState(snapshot=state.station_snapshot()))
            hub.to_observers(session_id, KioState(kio=state.kio))
            tick = TimerTick(timers=state.timers.snapshot())
            hub.to_observers(session_id, tick)
            hub.to_station(session_id, tick)

        case "call.resolve":
            # Курсант закрывает вызов не карточкой. Отдельное действие, а не
            # «положил трубку»: система должна отличить осознанное решение
            # от брошенного вызова (docs/spec/TICKETS.md).
            state.resolved_outcome = event.outcome.value
            state.resolve_comment = event.comment
            state.on_event("call.resolve")
            hub.to_observers(session_id, state.snapshot())

        case "callback.dial":
            state.on_event("callback.dial")

        case "self_assessment.submit":
            hub.record(session_id, SelfAssessed(list(event.missed), event.comment, now_utc()))
            state.self_assessed = True
            state.self_assessment = {"missed": event.missed, "comment": event.comment}
            await refresh_archived_report(session_id, state)
            # Оценка могла быть готова раньше самооценки — теперь её можно отдать.
            await release_score(session_id, state)

        case "call.hangup":
            await end_session(session_id, state, CallEndReason.HANGUP)


def _start_voice(session_id: UUID, state, *, initial_statement: bool = True) -> None:
    """Голос включается, когда курсант снял трубку: звонящий сразу кричит первую реплику."""
    models = get_voice_models()
    scenario = store.get(state.scenario_id)
    if models is None or scenario is None or state.voice is not None:
        return
    def send_audio(pcm: bytes) -> None:
        if state.recorder is not None:
            state.recorder.add_pcm(pcm, sample_rate=TTS_RATE)
        hub.to_trainee(session_id, pcm)

    state.voice = VoiceSession(
        session_id=session_id,
        state=state,
        models=models,
        send_event=lambda event: hub.to_trainee(session_id, event),
        send_observer=lambda event: hub.to_observers(session_id, event),
        send_audio=send_audio,
        persist=lambda entry: hub.commit(session_id, UtteranceAppended(entry)),
    )
    if scenario.background:
        event = BgStart(loop=scenario.background.loop, gain_db=scenario.background.gain_db)
        hub.broadcast(session_id, event)
    if initial_statement:
        state.voice.speak(scenario.first_line, state.persona.mood)


@router.websocket("/ws/call/{session_id}")
async def call(ws: WebSocket, session_id: UUID) -> None:
    # АРМ курсанта. Преподаватель допущен, чтобы показать приём вызова группе.
    entered = await session_socket(
        ws, session_id, (Role.TRAINEE, Role.INSTRUCTOR),
        not_found="Занятие ещё не запущено преподавателем",
    )
    if entered is None:
        return
    _who, state = entered

    with hub.trainee(session_id) as queue:
        if state.exercise is Exercise.CARD:
            from app.api.ws.control import card_briefing

            hub.to_trainee(session_id, card_briefing(state))
            if state.ended or state.dispatched_card is not None:
                hub.to_trainee(session_id, CallEnded(reason=state.end_reason or CallEndReason.COMPLETE))
            if state.score is not None:
                hub.to_trainee(session_id, ScoreReady(session_id=session_id))
        elif state.exercise is Exercise.CALL:
            scenario = state.scenario or store.get(state.scenario_id)
            required = ([field for field in state.required_fields if field != "dds"]
                        if scenario and scenario.ground_truth.incident_code
                        else list(state.required_fields))
            hub.to_trainee(session_id, CallIncoming(
                scenario_id=state.scenario_id, caller_number="+7 (495) 000-00-00",
                level=state.level, mode=state.mode, required_fields=required,
            ))
            hub.to_trainee(session_id, KioPatchOut(
                fields=state.kio.model_dump(mode="json"), source=PatchSource.OPERATOR,
            ))
            if state.started_at is not None:
                hub.to_trainee(session_id, CallStarted(started_at=state.started_at))
            if state.ended:
                hub.to_trainee(session_id, CallEnded(reason=state.end_reason or CallEndReason.HANGUP))
            if state.score is not None and state.self_assessed:
                hub.to_trainee(session_id, ScoreReady(session_id=session_id))
            hub.to_trainee(session_id, TimerTick(timers=state.timers.snapshot()))
            if state.started_at is not None and not state.ended and state.voice is None:
                # Rebuild non-serializable audio services after backend recovery;
                # the audio journal rehydrates the existing recording timeline.
                if state.recorder is None:
                    state.recorder = start_recording(session_id)
                _start_voice(session_id, state, initial_statement=False)
        writer = asyncio.create_task(pump(ws, queue))
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                # Бинарные кадры — аудио, текстовые — события. Направление определяется
                # каналом, обёртки JSON вокруг звука нет (docs/arch/CONTRACT.md).
                if message.get("bytes") is not None:
                    if state.exercise is Exercise.CALL:
                        _on_audio(session_id, state, message["bytes"])
                    else:
                        hub.to_trainee(session_id, ErrorEvent(
                            code=ErrorKind.UNSUPPORTED_EVENT,
                            message="Аудио не используется в этом упражнении",
                        ))
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
                # `_command_id` у `kio.patch` подтверждается эхом правки, а не
                # `CommandAck`: его нет в событиях курсанта, повторы не отсеиваются.
                if not await run_command(
                    ws, session_id, lambda: _handle(session_id, state, event), sender=writer,
                ):
                    return
        except WebSocketDisconnect:
            return
        finally:
            writer.cancel()
