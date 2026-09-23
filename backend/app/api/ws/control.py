"""Канал преподавателя: только передача.

**Ни одной команды, меняющей карточку курсанта.** Преподаватель управляет
ситуацией, а не работой обучаемого, иначе оценка перестаёт быть оценкой
курсанта (docs/arch/CONTRACT.md).

Ответы сюда не идут — канал односторонний. Всё, что сервер хочет сказать
преподавателю, уходит на его же сокет `observe`.
"""

import asyncio
import logging
import math
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.api.auth import audit, principal_of
from app.dialog.director import apply as apply_directive
from app.dialog.director import mood_of
from app.dialog.factory import build_caller
from app.dialog.persona import PersonaState
from app.dialog.runtime import get_embedder
from app.dialog.slots import SlotMachine
from app.domain.events import (
    CallEnded,
    CallEndReason,
    CallIncoming,
    CardBriefing,
    ErrorEvent,
    ErrorKind,
    Exercise,
    InstructorNoteShown,
    InstructorToServer,
    ModeSet,
    ReferenceStarted,
    ScoreReady,
    SessionEnded,
    StationState,
)
from app.domain.roles import Role
from app.domain.timers import TimerCode
from app.scenarios import store
from app.session.dds import prepare_queue
from app.session.hub import hub
from app.session.state import SessionState, now_utc
from app.voice.models import get_voice_models
from app.voice.pipeline import FILLERS, prefetch

log = logging.getLogger(__name__)
router = APIRouter()

_adapter = TypeAdapter(InstructorToServer)


def card_briefing(state: SessionState) -> CardBriefing:
    """Учебная текстовая вводная — исходные реплики, а не эталон карточки.

    В отсутствие диалога факты раскрываются сразу. Если факт уточняется,
    показываем и уточнение: иначе правильно заполнить карточку невозможно.
    """
    scenario = state.scenario
    lines = ["Учебная текстовая вводная: сведения заявителя приведены ниже.",
             scenario.first_line]
    for fact in scenario.facts:
        lines.append(f"• {fact.value}")
        if fact.refined:
            lines.append(f"  Уточнено: {fact.refined}")
    return CardBriefing(
        scenario_id=scenario.id, mode=state.mode, text="\n".join(lines),
        required_fields=([field for field in state.required_fields if field != "dds"]
                         if scenario.ground_truth.incident_code else list(state.required_fields)),
        card=state.kio,
        handoff_to_dds=state.handoff_to_dds,
    )


async def _start(session_id: UUID, event, who=None) -> None:
    if event.handoff_to_dds and event.exercise is not Exercise.CARD:
        hub.to_observers(session_id, ErrorEvent(
            code=ErrorKind.SCENARIO_INVALID,
            message="Передача в ДДС доступна только для текстовой карточки 112",
        ))
        return
    scenario = store.get(event.scenario_id)
    if scenario is None:
        hub.to_observers(
            session_id,
            ErrorEvent(code=ErrorKind.SCENARIO_INVALID, message=f"Нет сценария {event.scenario_id}"),
        )
        return

    scenario_ids = event.scenario_ids or [event.scenario_id]
    if event.exercise is Exercise.DDS:
        if not scenario_ids or scenario_ids[0] != event.scenario_id or len(scenario_ids) > 96:
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.SCENARIO_INVALID,
                message="Очередь ДДС должна начинаться с scenario_id и содержать не более 96 карточек",
            ))
            return
        scenarios = [store.get(scenario_id) for scenario_id in scenario_ids]
        if any(item is None for item in scenarios):
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.SCENARIO_INVALID, message="В очереди ДДС есть неизвестный сценарий",
            ))
            return
    else:
        scenarios = []

    attempt = 1
    recorded_trainee_id = event.trainee_id
    recorded_service = None
    if hub.journal:
        try:
            attempt, recorded_trainee_id, recorded_service = await hub.journal.start_lesson(
                session_id, scenario.id, event.mode.value, event.trainee, event.trainee_id,
                owner_login=who.login if who is not None else None,
            )
        except PermissionError:
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.FORBIDDEN,
                message="Занятие создано другим преподавателем",
            ))
            return

    # Занятие собирается целиком и только потом регистрируется: иначе
    # наблюдатель, подключившийся в эту щель, увидит полусобранное состояние
    # без слот-автомата и звонящего.
    state = SessionState(
        session_id=session_id,
        scenario_id=scenario.id,
        scenario_title=scenario.title,
        level=scenario.level.value,
        mode=event.mode,
        owner_login=who.login if who is not None else None,
        exercise=event.exercise,
        handoff_to_dds=event.handoff_to_dds,
        scenario=scenario.model_copy(deep=True),
        required_fields=scenario.required_fields,
        trainee_name=event.trainee,
        trainee_id=recorded_trainee_id,
        dds_service=recorded_service or event.dds_service,
        attempt=attempt,
        criteria=event.criteria,
    )
    state.timers.limits[TimerCode.DDS_ACK] = event.criteria.decision_time_limit_seconds * 1000
    if event.exercise is Exercise.CALL:
        embedder = get_embedder()
        if embedder is not None:
            state.slots = SlotMachine(state.scenario, embedder)
        state.persona = PersonaState(state.scenario.persona)
        state.caller = build_caller(
            scenario.id,
            use_pregenerated=scenario.tree.pregenerated,
        )
    elif event.exercise is Exercise.DDS:
        state.dds_scenarios = [item.model_copy(deep=True) for item in scenarios]
        prepare_queue(
            state,
            state.dds_scenarios,
            arrival_interval_seconds=event.dds_arrival_interval_seconds,
            max_waiting=event.dds_max_waiting,
        )
        state.started_at = state.dispatched_at
    else:
        state.started_at = now_utc()
    hub.register(state)
    if event.exercise is not Exercise.CALL and hub.journal and state.started_at is not None:
        await hub.journal.session_started(session_id, state.started_at)

    # Первая реплика и филлеры синтезируются, пока курсант не снял трубку:
    # «Алло! Помогите!» должно прозвучать мгновенно (docs/arch/BACKEND.md).
    if event.exercise is Exercise.CALL:
        models = get_voice_models()
        if models is not None:
            asyncio.create_task(prefetch(models, [scenario.first_line, *FILLERS.values()]))
        state.on_event("call.incoming")
    await hub.checkpoint(session_id)
    hub.start_ticker(session_id)
    if who is not None:
        # Запуск занятия меняет чужой результат — значит попадает в журнал
        # аудита (ТЗ, хранение не менее шести месяцев).
        # Сохраняем до продолжения сценария, чтобы завершение процесса не
        # потеряло событие. ФИО курсанта в долгоживущий журнал не дублируем.
        await audit(
            who.login,
            who.role.value,
            "lesson.start",
            str(session_id),
            f"{scenario.id}, режим {event.mode.value}",
        )

    if event.exercise is Exercise.CALL:
        hub.to_trainee(
            session_id,
            CallIncoming(
                scenario_id=scenario.id,
                caller_number="+7 (495) 000-00-00",
                level=scenario.level,
                mode=event.mode,
                required_fields=[field for field in scenario.required_fields
                                 if field != "dds" or not scenario.ground_truth.incident_code],
            ),
        )
    elif event.exercise is Exercise.DDS:
        hub.to_station(session_id, state.card_received_event())
        hub.to_station(session_id, StationState(snapshot=state.station_snapshot()))
    else:
        hub.to_trainee(session_id, card_briefing(state))
    hub.to_observers(session_id, ModeSet(mode=event.mode))
    hub.to_observers(session_id, state.snapshot())


async def _stop(session_id: UUID) -> None:
    state = hub.get(session_id)
    if state is None or state.ended:
        return
    state.ended_at = now_utc()
    state.end_reason = CallEndReason.INSTRUCTOR
    if state.voice is not None:
        await state.voice.close()
    hub.stop_ticker(session_id)
    hub.to_observers(session_id, SessionEnded(reason=CallEndReason.INSTRUCTOR))
    from app.session.finish import finish

    if state.exercise in {Exercise.DDS, Exercise.CARD}:
        if state.exercise is Exercise.DDS or state.handoff_to_dds and state.dispatched_card is not None:
            hub.to_station(session_id, SessionEnded(reason=CallEndReason.INSTRUCTOR))
        else:
            hub.to_trainee(session_id, CallEnded(reason=CallEndReason.INSTRUCTOR))
        if state.exercise is Exercise.DDS or state.handoff_to_dds and state.dispatched_card is not None:
            hub.to_station(session_id, ScoreReady(session_id=session_id))
    else:
        hub.to_trainee(session_id, CallEnded(reason=CallEndReason.INSTRUCTOR))
    await finish(session_id, state)
    if hub.journal:
        await hub.journal.session_ended(session_id, state.ended_at, CallEndReason.INSTRUCTOR.value)


async def _reject(ws: WebSocket, message: str) -> None:
    """Отказ до входа в цикл: сокет закрывается с объяснением, а не молча."""
    await ws.send_text(
        ErrorEvent(code=ErrorKind.FORBIDDEN, message=message).model_dump_json()
    )
    await ws.close()


@router.websocket("/ws/control/{session_id}")
async def control(ws: WebSocket, session_id: UUID) -> None:
    await ws.accept()

    # Пульт преподавателя: управление занятием доступно только ему.
    who = principal_of(ws)
    if who is None or who.role not in (Role.INSTRUCTOR,):
        await _reject(ws, "Недостаточно прав для этого экрана")
        return
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

            existing_state = hub.get(session_id)
            if event.type == "scenario.start":
                if existing_state is not None:
                    hub.to_observers(session_id, ErrorEvent(
                        code=ErrorKind.FORBIDDEN,
                        message="Для нового занятия создайте новый номер сессии",
                    ))
                    continue
            elif existing_state is None or existing_state.owner_login != who.login:
                hub.to_observers(session_id, ErrorEvent(
                    code=ErrorKind.FORBIDDEN,
                    message="Это занятие принадлежит другому преподавателю",
                ))
                continue

            match event.type:
                case "scenario.start":
                    await _start(session_id, event, who)
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
                    if event.session_id != session_id:
                        hub.to_observers(session_id, ErrorEvent(
                            code=ErrorKind.FORBIDDEN,
                            message="Оценка относится к другому занятию",
                        ))
                        continue
                    state = hub.get(session_id)
                    if state is not None and state.score is not None:
                        try:
                            verdict = float(event.verdict)
                        except ValueError:
                            verdict = float("nan")
                        if not math.isfinite(verdict) or not 0 <= verdict <= 100:
                            hub.to_observers(session_id, ErrorEvent(
                                code=ErrorKind.UNSUPPORTED_EVENT,
                                message="Оценка должна быть числом от 0 до 100",
                            ))
                            continue
                        # Автооценка остаётся рядом: видно, что скорректировано и кем.
                        state.score = {
                            **state.score,
                            "score_final": verdict,
                            "overridden_by": who.login,
                            "override_comment": event.comment,
                        }
                        if hub.journal:
                            await hub.journal.score_override(
                                session_id, verdict, who.login, event.comment
                            )
                        await audit(
                            who.login, who.role.value, "score.override", str(session_id),
                            f"{state.score.get('score_auto')} → {verdict}: {event.comment}",
                        )
                        hub.to_observers(session_id, ScoreReady(session_id=session_id))
                case "director.inject":
                    state = hub.get(session_id)
                    if state is None:
                        continue
                    if state.exercise is not Exercise.CALL:
                        hub.to_observers(session_id, ErrorEvent(
                            code=ErrorKind.UNSUPPORTED_EVENT,
                            message="Директивы звонящему доступны только в голосовом упражнении",
                        ))
                        continue
                    result = apply_directive(state, event.directive)
                    if result.needs_network:
                        hub.to_observers(session_id, ErrorEvent(
                            code=ErrorKind.DIRECTIVE_NEEDS_NETWORK,
                            message="Свободный текст требует локально запущенной модели; без неё доступны кнопки",
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
            await hub.checkpoint(session_id)
    except WebSocketDisconnect:
        return
