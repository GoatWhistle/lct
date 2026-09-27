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
import secrets
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from app.api.ws.session import close_fenced, run_command, session_socket
from app.config import get_settings
from app.db.base import get_sessionmaker
from app.db.repo import SessionNodeConflict
from app.dialog.director import apply as apply_directive
from app.dialog.director import mood_of
from app.dialog.factory import build_caller
from app.dialog.persona import PersonaState
from app.dialog.runtime import get_embedder
from app.dialog.slots import SlotMachine
from app.domain.classifiers import Outcome
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
    SessionEnded,
    StationState,
)
from app.domain.roles import Role
from app.domain.timers import TimerCode
from app.scenarios import store
from app.session.dds import prepare_queue
from app.session.access import can_access
from app.session.hub import hub
from app.session.finish import end_session, override_score
from app.session.state import SessionState, now_utc
from app.session.store import LessonIdentity, LessonRequest, NoteAdded, ScoreOverridden
from app.voice.models import get_voice_models
from app.voice.pipeline import FILLERS, prefetch

log = logging.getLogger(__name__)
router = APIRouter()

_adapter = TypeAdapter(InstructorToServer)

# Пульт может молчать; без входящих кадров потеря lease замечается этим опросом.
_FENCE_POLL_SECONDS = 1.0


def _dds_ineligible_scenarios(scenarios):
    """Консультация и передача региона не являются готовыми карточками ДДС."""
    return [scenario for scenario in scenarios if scenario.outcome is not Outcome.CARD]


def card_briefing(state: SessionState) -> CardBriefing:
    """Первую реплику показывает курсант; факты раскрываются только в ответах."""
    scenario = state.scenario
    return CardBriefing(
        scenario_id=scenario.id, mode=state.mode, text=scenario.first_line,
        required_fields=([field for field in state.required_fields if field != "dds"]
                         if scenario.ground_truth.incident_code else list(state.required_fields)),
        card=state.kio,
        handoff_to_dds=state.handoff_to_dds,
    )


def _build_state(session_id: UUID, event, who, scenario, scenarios,
                 identity: LessonIdentity) -> SessionState:
    """Занятие собирается целиком и только потом регистрируется: иначе
    наблюдатель, подключившийся в эту щель, увидит полусобранное состояние
    без слот-автомата и звонящего. Снимок пишется в той же транзакции, что
    и строка занятия."""
    state = SessionState(
        session_id=session_id,
        scenario_id=scenario.id,
        scenario_title=scenario.title,
        level=scenario.level.value,
        mode=event.mode,
        owner_login=who.login if who is not None else None,
        backend_fencing_epoch=identity.fencing_epoch,
        exercise=event.exercise,
        handoff_to_dds=event.handoff_to_dds,
        scenario=scenario.model_copy(deep=True),
        required_fields=scenario.required_fields,
        trainee_name=event.trainee,
        trainee_id=identity.trainee_id,
        dds_service=identity.service or event.dds_service,
        attempt=identity.attempt,
        criteria=event.criteria,
    )
    state.timers.limits[TimerCode.DDS_ACK] = event.criteria.decision_time_limit_seconds * 1000
    state.timers.limits[TimerCode.CARD_FILL] = event.criteria.card_fill_time_limit_seconds * 1000
    state.timers.limits[TimerCode.DDS_WORK] = event.criteria.dds_card_work_time_limit_seconds * 1000
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
        prepare_queue(
            state,
            [item.model_copy(deep=True) for item in scenarios],
            arrival_interval_seconds=event.dds_arrival_interval_seconds,
            max_waiting=event.dds_max_waiting,
        )
        state.started_at = state.desk.active.dispatched_at if state.desk.active else None
    else:
        state.started_at = now_utc()
        if event.exercise is Exercise.CARD:
            embedder = get_embedder()
            if embedder is not None:
                state.slots = SlotMachine(state.scenario, embedder)
            state.persona = PersonaState(state.scenario.persona)
            state.caller = build_caller(
                scenario.id, use_pregenerated=scenario.tree.pregenerated,
            )
            state.on_event("card.start")
        if event.handoff_to_dds:
            state.pending_dds_scenarios = [item.model_copy(deep=True) for item in scenarios[1:]]
            state.desk.arrival_interval_seconds = event.dds_arrival_interval_seconds
            state.desk.max_waiting = event.dds_max_waiting
    if event.exercise is Exercise.CALL:
        state.on_event("call.incoming")
    return state


async def _start(session_id: UUID, event, who=None) -> None:
    if event.handoff_to_dds and event.exercise is not Exercise.CARD:
        hub.to_observers(session_id, ErrorEvent(
            code=ErrorKind.SCENARIO_INVALID,
            message="Передача в ДДС доступна только для текстовой карточки 112",
        ))
        return
    scenario_ids = list(dict.fromkeys([
        event.scenario_id, *(event.scenario_ids or []), *(event.random_scenario_ids or []),
    ]))
    if get_settings().demo_no_db or not hub.store.persistent:
        catalog = {
            scenario_id: scenario for scenario_id in scenario_ids
            if (scenario := store.get(scenario_id)) is not None
        }
        hidden_scenario_ids = (
            await store.scenario_ids_owned_by_other(None, who.login)
            if who is not None else set()
        )
    else:
        try:
            async with get_sessionmaker()() as db:
                catalog, hidden_scenario_ids = await store.published_catalog(
                    db, scenario_ids, who.login if who is not None else None,
                )
        except Exception as exc:
            # Avoid serializing scenario facts or SQL bind values into application logs.
            log.error("не удалось разрешить сценарий из общей библиотеки (%s)", type(exc).__name__)
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.INTERNAL,
                message="Не удалось проверить сценарий в общей библиотеке; запуск отменён.",
            ))
            return
    scenario = catalog.get(event.scenario_id)
    if scenario is None:
        hub.to_observers(
            session_id,
            ErrorEvent(code=ErrorKind.SCENARIO_INVALID, message=f"Нет сценария {event.scenario_id}"),
        )
        return
    if scenario.id in hidden_scenario_ids:
        hub.to_observers(session_id, ErrorEvent(
            code=ErrorKind.SCENARIO_INVALID,
            message="Сценарий не найден или недоступен этому преподавателю",
        ))
        return

    if event.random_scenario_ids:
        if len(event.random_scenario_ids) > 96:
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.SCENARIO_INVALID,
                message="Случайный отбор ограничен 96 карточками",
            ))
            return
        pool_ids = list(dict.fromkeys(event.random_scenario_ids))
        pool = [catalog.get(scenario_id) for scenario_id in pool_ids]
        if any(item is None for item in pool):
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.SCENARIO_INVALID,
                message="В случайном отборе есть неизвестный сценарий",
            ))
            return
        if hidden_scenario_ids.intersection(pool_ids):
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.SCENARIO_INVALID,
                message="Сценарий не найден или недоступен этому преподавателю",
            ))
            return
        ineligible_pool = _dds_ineligible_scenarios(pool)
        if ineligible_pool:
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.SCENARIO_INVALID,
                message="Случайный отбор должен содержать только готовые карточки ДДС",
            ))
            return
        scenario = secrets.choice(pool)

    scenario_ids = event.scenario_ids or [event.scenario_id]
    if event.exercise is Exercise.DDS or event.handoff_to_dds:
        if (not scenario_ids or scenario_ids[0] != event.scenario_id
                or len(scenario_ids) > 96):
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.SCENARIO_INVALID,
                message="Очередь ДДС должна начинаться с scenario_id и содержать не более 96 карточек",
            ))
            return
        if event.random_scenario_ids:
            extra_ids = list(dict.fromkeys(
                item for item in scenario_ids[1:] if item != scenario.id
            ))
            extras = [catalog.get(item) for item in extra_ids]
            remaining_random = [item for item in pool if item.id != scenario.id
                                and item.id not in extra_ids]
            randomized_tail = secrets.SystemRandom().sample(
                remaining_random, k=len(remaining_random)
            )
            scenarios = [scenario, *extras, *randomized_tail]
        else:
            scenarios = [catalog.get(scenario_id) for scenario_id in scenario_ids]
        if len(scenarios) > 96:
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.SCENARIO_INVALID,
                message="Очередь ДДС не может содержать более 96 карточек",
            ))
            return
        if any(item is None for item in scenarios):
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.SCENARIO_INVALID, message="В очереди ДДС есть неизвестный сценарий",
            ))
            return
        if any(item.id in hidden_scenario_ids for item in scenarios):
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.SCENARIO_INVALID,
                message="Сценарий не найден или недоступен этому преподавателю",
            ))
            return
        ineligible = _dds_ineligible_scenarios(scenarios)
        if ineligible:
            titles = ", ".join(item.title for item in ineligible)
            hub.to_observers(session_id, ErrorEvent(
                code=ErrorKind.SCENARIO_INVALID,
                message=("В очередь ДДС можно добавить только готовые карточки с исходом "
                         f"«карточка и передача в ДДС». Исключите: {titles}"),
            ))
            return
    else:
        scenarios = []

    try:
        state = await hub.store.open(LessonRequest(
            session_id=session_id, scenario_id=scenario.id, mode=event.mode.value,
            trainee_name=event.trainee, trainee_id=event.trainee_id,
            owner_login=who.login if who is not None else None,
            backend_node_id=get_settings().backend_node_id,
        ), lambda identity: _build_state(session_id, event, who, scenario, scenarios, identity))
    except SessionNodeConflict:
        hub.to_observers(session_id, ErrorEvent(
            code=ErrorKind.FORBIDDEN,
            message="Сессия закреплена за другим backend-узлом; проверьте маршрутизацию proxy",
        ))
        return
    except PermissionError:
        hub.to_observers(session_id, ErrorEvent(
            code=ErrorKind.FORBIDDEN,
            message="Занятие создано другим преподавателем",
        ))
        return
    except Exception as exc:  # noqa: BLE001 — без долговечной истории занятие не стартует
        log.error("занятие %s не записано (%s)", session_id, type(exc).__name__)
        hub.to_observers(session_id, ErrorEvent(
            code=ErrorKind.INTERNAL,
            message="Не удалось записать занятие и аудит; запуск отменён.",
        ))
        return
    hub.register(state)

    # Первая реплика и филлеры синтезируются, пока курсант не снял трубку:
    # «Алло! Помогите!» должно прозвучать мгновенно (docs/arch/BACKEND.md).
    if event.exercise is Exercise.CALL:
        models = get_voice_models()
        if models is not None:
            asyncio.create_task(prefetch(models, [scenario.first_line, *FILLERS.values()]))
    hub.start_ticker(session_id)
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
    if state is None:
        return
    await end_session(session_id, state, CallEndReason.INSTRUCTOR)


async def _command(session_id: UUID, event, who) -> None:
    """Команда пульта. Всё, кроме запуска, идёт внутри `hub.operation`."""
    state = hub.get(session_id)
    match event.type:
        case "scenario.start":
            await _start(session_id, event, who)
        case "session.stop":
            await _stop(session_id)
        case "instructor_note.add":
            if state is not None:
                state.notes.append({
                    "type": "instructor_note.shown",
                    "transcript_ref": event.transcript_ref,
                    "text": event.text,
                    "author": "преподаватель",
                })
                hub.record(session_id, NoteAdded(event.transcript_ref, event.text, "преподаватель"))
            hub.to_observers(
                session_id,
                InstructorNoteShown(
                    transcript_ref=event.transcript_ref,
                    text=event.text,
                    author="преподаватель",
                ),
            )
        case "reference.play":
            if state is not None:
                hub.to_observers(session_id, ReferenceStarted(scenario_id=state.scenario_id))
        case "score.override":
            if event.session_id != session_id:
                hub.to_observers(session_id, ErrorEvent(
                    code=ErrorKind.FORBIDDEN,
                    message="Оценка относится к другому занятию",
                ))
                return
            if state is None or state.score is None:
                return
            try:
                verdict = float(event.verdict)
            except ValueError:
                verdict = float("nan")
            if not math.isfinite(verdict) or not 0 <= verdict <= 100:
                hub.to_observers(session_id, ErrorEvent(
                    code=ErrorKind.UNSUPPORTED_EVENT,
                    message="Оценка должна быть числом от 0 до 100",
                ))
                return
            override_score(state, ScoreOverridden(
                score_final=verdict, author=who.login, role=who.role.value,
                comment=event.comment,
            ))
        case "director.inject":
            if state is None:
                return
            if state.exercise is not Exercise.CALL:
                hub.to_observers(session_id, ErrorEvent(
                    code=ErrorKind.UNSUPPORTED_EVENT,
                    message="Директивы звонящему доступны только в голосовом упражнении",
                ))
                return
            result = apply_directive(state, event.directive)
            if result.needs_network:
                hub.to_observers(session_id, ErrorEvent(
                    code=ErrorKind.DIRECTIVE_NEEDS_NETWORK,
                    message="Свободный текст требует локально запущенной модели; без неё доступны кнопки",
                ))
                return
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


@router.websocket("/ws/control/{session_id}")
async def control(ws: WebSocket, session_id: UUID) -> None:
    # Пульт преподавателя: управление занятием доступно только ему. Занятия
    # до `scenario.start` ещё нет — доступ проверяется на каждой команде.
    entered = await session_socket(ws, session_id, (Role.INSTRUCTOR,), require_state=False)
    if entered is None:
        return
    who, _state = entered
    # Чтение живёт дольше тика опроса: wait_for отменял бы его, а отмена после
    # того, как receive уже забрал кадр, теряет команду или websocket.disconnect.
    # Команда обрабатывается здесь, одной операцией хранилища.
    read: asyncio.Future | None = None
    try:
        while True:
            if read is None:
                read = asyncio.ensure_future(ws.receive_json())
            done, _ = await asyncio.wait({read}, timeout=_FENCE_POLL_SECONDS)
            if not done:
                if hub.is_lease_fenced(session_id):
                    await close_fenced(ws)
                    return
                continue
            payload, read = read.result(), None
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
                # Запуск пишется своей транзакцией в `store.open`: до неё занятия нет.
                await _command(session_id, event, who)
                continue
            if existing_state is None or not can_access(who, existing_state):
                hub.to_observers(session_id, ErrorEvent(
                    code=ErrorKind.FORBIDDEN,
                    message="Это занятие принадлежит другому преподавателю",
                ))
                continue
            if not await run_command(ws, session_id, lambda: _command(session_id, event, who)):
                return
    except WebSocketDisconnect:
        return
    finally:
        if read is not None:
            read.cancel()
