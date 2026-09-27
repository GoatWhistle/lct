"""Пауза занятия с пульта преподавателя (lct-39).

Часы — управляемый монотонный счётчик: тест продвигает их сам, а не спит,
проверяя ровно то, что требует карточка — пауза не считается в норматив,
переживает чекпойнт и пишется в аудит.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.http import sessions as sessions_http
from app.api.ws import control as control_ws
from app.api.ws import call as call_ws
from app.api.ws import station as station_ws
from app.domain.classifiers import DDSCode
from app.domain.events import (
    CallbackDial,
    CallEnded,
    CallEndReason,
    CardAck,
    DdsDispatch,
    DirectorInject,
    ErrorEvent,
    Exercise,
    HintRequest,
    KioPatchIn,
    LessonCriteria,
    Principal,
    SelfAssessmentSubmit,
    SessionMode,
    SessionPause,
    SessionPaused,
    SessionResume,
    TtsCancel,
    TtsEnd,
)
from app.domain.roles import Role
from app.domain.statuses import PhoneReportRecord, ServiceStatus
from app.domain.timers import TimerCode
from app.main import app
from app.scenarios.loader import load_file
from app.scoring.dispatcher import dispatcher_metrics
from app.session import timers as timers_module
from app.session.checkpoint import dump_state, load_state
from app.session.finish import end_session, score_dds_card
from app.session.hub import hub
from app.session.state import SessionState, now_utc
from app.session.store import LessonPaused, LessonResumed, MemorySessionStore
from app.session.timers import Timer

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"
TEACHER = Principal(login="teacher", full_name="Преподаватель", role=Role.INSTRUCTOR)


def _dds_state_with_active_card(clock, criteria_ms: int = 30_000):
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
        scenario=scenario, owner_login="teacher",
    )
    state.timers.limits[TimerCode.DDS_ACK] = criteria_ms
    from app.session.dds import prepare_queue

    prepare_queue(state, [scenario.model_copy(deep=True)])
    return state, state.desk.active


class FakeClock:
    """Управляемая замена `time.monotonic`: тест продвигает время явно."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(timers_module.time, "monotonic", fake)
    return fake


def test_timer_pause_freezes_elapsed_until_resume(clock):
    timer = Timer(code=TimerCode.DDS_ACK)
    timer.start(clock.now)
    clock.advance(10)

    timer.pause(clock.now)
    frozen = timer.current_ms(clock.now)
    clock.advance(60)  # долгая пауза — часы идут, таймер не должен

    assert timer.current_ms(clock.now) == frozen == 10_000

    timer.resume(clock.now)
    clock.advance(15)

    assert timer.current_ms(clock.now) == 25_000


def test_session_pause_excludes_wait_from_dds_ack_deadline(clock):
    """Карточка: 30 с норматива. Пауза 60 с внутри окна не должна дать D1 —
    карточка открыта на 25-й секунде активного времени, а не 85-й реальной."""
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
        scenario=scenario, criteria=LessonCriteria(decision_time_limit_seconds=30),
    )
    state.timers.limits[TimerCode.DDS_ACK] = 30_000
    from app.session.dds import prepare_queue
    prepare_queue(state, [scenario.model_copy(deep=True)])
    card = state.desk.active

    clock.advance(10)
    state.pause()
    clock.advance(60)
    state.resume()
    clock.advance(15)
    card.on_event("dds.open")

    measured = card.timers.measured_ms(TimerCode.DDS_ACK)
    assert measured == 25_000
    assert measured <= card.timers.limits[TimerCode.DDS_ACK]


def test_pause_freezes_queue_arrival_and_resume_shifts_it(clock, monkeypatch):
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    second = scenario.model_copy(deep=True)
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
        scenario=scenario,
    )
    from app.session.dds import prepare_queue
    prepare_queue(state, [scenario, second], arrival_interval_seconds=30, max_waiting=3)

    due_before = state.desk.next_arrival_at
    assert due_before is not None

    base = now_utc()
    monkeypatch.setattr("app.session.state.now_utc", lambda: base)
    state.pause()
    idle = timedelta(minutes=1)
    monkeypatch.setattr("app.session.state.now_utc", lambda: base + idle)
    state.resume()

    # Следующая карточка не должна поступить раньше, чем простояла пауза.
    assert state.desk.next_arrival_at == due_before + idle
    assert state.desk.deliver_due(due_before + timedelta(seconds=1)) == 0


def test_checkpoint_round_trip_keeps_pause_without_adding_downtime(clock):
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
        scenario=scenario,
    )
    from app.session.dds import prepare_queue
    prepare_queue(state, [scenario.model_copy(deep=True)])
    card = state.desk.active

    clock.advance(5)
    state.pause()
    saved_at = now_utc()

    payload = dump_state(state)
    # Процесс лежал минуту — по часам сервера, не по monotonic этого теста.
    restored_at = saved_at + timedelta(minutes=1)
    restored = load_state(payload, restored_at)

    assert restored.paused is True
    restored_card = restored.desk.active
    restored_timer = restored_card.timers.timers[TimerCode.DDS_ACK]
    # Простоя в счёт не пошло: элапсед остался тем же, что был на паузе.
    assert restored_timer.elapsed_ms == card.timers.timers[TimerCode.DDS_ACK].elapsed_ms
    assert restored_timer.started_at is None

    # После takeover пауза всё ещё действует; лишь явное возобновление
    # продолжает норматив с пяти секунд до прерывания.
    restored.resume()
    clock.advance(10)
    assert restored_timer.current_ms(clock.now) == 15_000


def test_pause_and_resume_are_recorded_for_audit(monkeypatch):
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
        scenario=scenario, owner_login="teacher",
    )
    from app.session.dds import prepare_queue
    prepare_queue(state, [scenario.model_copy(deep=True)])

    store = MemorySessionStore()
    monkeypatch.setitem(hub._sessions, state.session_id, state)
    monkeypatch.setattr(hub, "store", store)

    async def run():
        async with hub.operation(state.session_id):
            await control_ws._command(state.session_id, SessionPause(), TEACHER)
        async with hub.operation(state.session_id):
            await control_ws._command(state.session_id, SessionResume(), TEACHER)

    asyncio.run(run())

    records = [record for _sid, batch in store.commits for record in batch]
    assert any(isinstance(record, LessonPaused) for record in records)
    assert any(isinstance(record, LessonResumed) for record in records)
    assert state.paused is False


def test_dispatcher_metric_dds_ack_excludes_pause_from_elapsed(clock):
    """Регрессия: `dispatcher_metrics` считала `primary.at - dispatched_at` —
    календарную разницу, которую пауза не трогает. Метрика должна брать то
    же замороженное время, что и таймер на экране."""
    state, card = _dds_state_with_active_card(clock)
    services = state.card_services(card)
    service = services[0]

    clock.advance(10)
    state.pause()
    clock.advance(60)
    state.resume()
    clock.advance(15)
    card.on_event("dds.open")
    card.set_service_status(service, ServiceStatus.ACCEPTED, "Принято")

    metric = next(m for m in dispatcher_metrics(card, 30_000, services=services) if m.key == "dds_ack")
    assert metric.passed is True
    assert "25.0" in metric.fact


def test_ending_session_while_paused_finalizes_pause_and_unblocks_report(clock, monkeypatch):
    """Регрессия: занятие, завершённое прямо на паузе, оставляло `paused=True`
    навсегда — самооценка и отчёт отклонялись баннером «Пауза», а простой,
    не закрытый явным `session.resume`, не попадал в `total_paused_ms`."""
    from app.session import finish as finish_module

    async def no_coach(_metrics):
        class NoCoaching:
            def model_dump(self, **_kwargs):
                return {}
        return NoCoaching()

    monkeypatch.setattr(finish_module, "coach", no_coach)

    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING, exercise=Exercise.CALL,
        scenario=scenario, owner_login="teacher",
    )
    store = MemorySessionStore()
    monkeypatch.setattr(hub, "store", store)
    monkeypatch.setitem(hub._sessions, state.session_id, state)

    base = now_utc()
    monkeypatch.setattr("app.session.state.now_utc", lambda: base)
    clock.advance(5)
    state.pause()
    clock.advance(30)  # преподаватель завершает занятие, не возобновив его
    idle = timedelta(seconds=45)
    monkeypatch.setattr("app.session.state.now_utc", lambda: base + idle)

    events = []

    async def run():
        with hub.trainee(state.session_id) as queue:
            async with hub.operation(state.session_id):
                await end_session(state.session_id, state, CallEndReason.INSTRUCTOR)
            async with hub.operation(state.session_id):
                await call_ws._handle(
                    state.session_id, state,
                    SelfAssessmentSubmit(missed=[], comment="Проверил"),
                )
            while not queue.empty():
                events.append(queue.get_nowait())

    asyncio.run(run())

    assert state.ended
    assert state.paused is False
    assert state.total_paused_ms >= 30_000
    assert state.self_assessed
    assert state.score is not None
    assert state.score["full_report"]["total_paused_ms"] >= 30_000
    assert any(isinstance(event, SessionPaused) and not event.paused for event in events)
    # Снятие паузы приходит после `call.ended`: иначе клиент дошлёт отложенные
    # на паузе правки в уже закрытое занятие.
    kinds = [type(event) for event in events]
    assert kinds.index(CallEnded) < kinds.index(SessionPaused)
    assert any(isinstance(record, LessonResumed) for _sid, batch in store.commits for record in batch)


def test_dds_work_overdue_survives_pause():
    """Регрессия: `dds_work_overdue_cards` требовал `started_at is not None`,
    который пауза обнуляет — уже случившееся нарушение исчезало из реестра."""
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    from app.session.dds import build_card
    from app.session.timers import SessionTimers

    limits = {**SessionTimers().limits, TimerCode.DDS_WORK: 1_000}
    card = build_card(scenario, 0, limits)
    timer = card.timers.timers.setdefault(TimerCode.DDS_WORK, Timer(code=TimerCode.DDS_WORK))
    timer.start(time.monotonic() - 5.0)  # уже превысил норматив в 1 с
    timer.pause(time.monotonic())

    overdue = (
        timer.started_at is not None or timer.paused
    ) and not timer.stopped and timer.current_ms(time.monotonic()) > limits[TimerCode.DDS_WORK]
    assert overdue is True


def test_active_registry_keeps_existing_overdue_during_pause(http_client, clock):
    app.dependency_overrides[sessions_http.optional_session] = lambda: None
    state, card = _dds_state_with_active_card(clock)
    state.owner_login = "dev"
    timer = card.timers.timers.setdefault(TimerCode.DDS_WORK, Timer(code=TimerCode.DDS_WORK))
    card.timers.limits[TimerCode.DDS_WORK] = 1_000
    timer.start(clock.now)
    clock.advance(5)
    state.pause()
    hub._sessions[state.session_id] = state
    try:
        response = http_client.get("/api/sessions/active")
        assert response.status_code == 200
        row = next(item for item in response.json() if item["session_id"] == str(state.session_id))
        assert row["dds_work_overdue_cards"] == 1
        assert row["paused"] is True
    finally:
        hub._sessions.pop(state.session_id, None)
        app.dependency_overrides.pop(sessions_http.optional_session, None)


@pytest.fixture
def http_client(monkeypatch):
    async def audit_in_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sessions_http, "audit_required", audit_in_memory)
    with TestClient(app) as test_client:
        test_client.post("/api/auth/dev-token")
        hub.store = MemorySessionStore()
        yield test_client


def _wait(predicate, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("не дождались")


def test_paused_socket_drops_audio_instead_of_feeding_voice(http_client):
    """Регрессия: пауза блокировала только JSON-команды — бинарные аудиокадры
    /ws/call всё равно доходили до счётчика и голосового контура."""
    session_id = uuid4()
    with http_client.websocket_connect(f"/ws/control/{session_id}") as control:
        control.send_json({
            "type": "scenario.start", "scenario_id": "fire-apartment-l2",
            "trainee": "Иванов И.И.", "mode": "training",
        })
        _wait(lambda: hub.get(session_id))
        state = hub.get(session_id)

        with http_client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            trainee.send_bytes(b"\x00\x00" * 320)
            _wait(lambda: state.audio_frames == 1)

            control.send_json({"type": "session.pause"})
            _wait(lambda: state.paused)

            trainee.send_bytes(b"\x00\x00" * 320)
            trainee.send_bytes(b"\x00\x00" * 320)
            time.sleep(0.1)  # кадры паузы не должны просочиться в счётчик

    assert state.audio_frames == 1


def test_reconnect_to_call_replays_paused_banner(http_client):
    """Регрессия: переподключение к /ws/call не сообщало о текущей паузе —
    баннер появлялся только после следующего `session.pause`."""
    session_id = uuid4()
    with http_client.websocket_connect(f"/ws/control/{session_id}") as control:
        control.send_json({
            "type": "scenario.start", "scenario_id": "fire-apartment-l2",
            "trainee": "Иванов И.И.", "mode": "training",
        })
        _wait(lambda: hub.get(session_id))
        control.send_json({"type": "session.pause"})
        _wait(lambda: hub.get(session_id).paused)

        with http_client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            messages = [trainee.receive_json() for _ in range(2)]
            assert any(
                message["type"] == "session.paused" and message["paused"]
                for message in messages
            )

        control.send_json({"type": "session.resume"})
        _wait(lambda: not hub.get(session_id).paused)
        with http_client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            message = trainee.receive_json()
            assert message == {"type": "session.paused", "paused": False}


@pytest.mark.asyncio
async def test_pause_cancels_inflight_voice_and_clears_buffer(monkeypatch):
    from app.voice import pipeline

    class FakeVad:
        def __init__(self, *_args, **_kwargs):
            self.reset_count = 0

        def reset(self):
            self.reset_count += 1

        def push(self, _frame):
            return []

    monkeypatch.setattr(pipeline, "StreamingVad", FakeVad)
    state, _card = _dds_state_with_active_card(lambda: 1_000.0)
    events = []
    audio = []
    started = asyncio.Event()
    release = asyncio.Event()

    class Models:
        vad_path = None

    voice = pipeline.VoiceSession(
        state.session_id, state, Models(), events.append, lambda _event: None, audio.append,
    )
    state.voice = voice

    async def slow_synthesis(_text):
        started.set()
        await release.wait()
        return b"\x00\x00" * 2_400

    monkeypatch.setattr(voice, "synthesize", slow_synthesis)
    try:
        speaking = voice.speak("Алло, помогите!", state.persona.mood if state.persona else pipeline.Mood.PANIC)
        await asyncio.wait_for(started.wait(), timeout=1)
        voice._queue.put_nowait((b"old", time.monotonic()))
        state.pause()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await speaking
        await asyncio.sleep(0)
        assert any(isinstance(event, TtsCancel) and event.reason == "director" for event in events)
        assert not any(isinstance(event, TtsEnd) for event in events)
        assert audio == []
        assert voice._queue.empty()
        assert voice._vad.reset_count == 1
    finally:
        await voice.close()


def _call_state():
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    return SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING, exercise=Exercise.CALL,
        scenario=scenario, owner_login="teacher",
    )


def _drain(queue) -> list:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def test_timer_start_on_pause_keeps_frozen_elapsed(clock):
    """Повторное событие запуска на паузе не должно пустить часы с нуля."""
    timer = Timer(code=TimerCode.CALLBACK)
    timer.start(clock.now)
    clock.advance(4)
    timer.pause(clock.now)

    timer.start(clock.now)
    clock.advance(120)

    assert timer.paused is True
    assert timer.current_ms(clock.now) == 4_000


def test_director_inject_rejected_while_paused(clock, monkeypatch):
    """Регрессия: обрыв связи на паузе запускал норматив обратного дозвона,
    и весь простой уходил в него; курсант получал `call.ended` под баннером."""
    state = _call_state()
    monkeypatch.setattr(hub, "store", MemorySessionStore())
    monkeypatch.setitem(hub._sessions, state.session_id, state)
    state.on_event("call.incoming")
    state.on_event("call.answer")
    state.pause()

    async def run():
        with hub.observer(state.session_id) as observer, hub.trainee(state.session_id) as trainee:
            async with hub.operation(state.session_id):
                await control_ws._command(
                    state.session_id, DirectorInject(directive="line_dropped"), TEACHER,
                )
            return _drain(observer), _drain(trainee)

    observed, to_trainee = asyncio.run(run())
    clock.advance(120)

    assert any(isinstance(event, ErrorEvent) for event in observed)
    assert to_trainee == []
    assert state.directives == []
    assert TimerCode.CALLBACK not in state.timers.timers
    assert state.timers.timers[TimerCode.INTERVIEW].paused is True


@pytest.mark.parametrize("event", [
    KioPatchIn(fields={"address": "ул. Ленина, 1"}),
    HintRequest(),
    DdsDispatch(service=DDSCode.FIRE),
    CallbackDial(),
], ids=lambda event: event.type)
def test_call_channel_rejects_trainee_actions_while_paused(clock, monkeypatch, event):
    """Истина — сервер: кнопки на фронте выключены, но команда с паузы
    не должна ни менять карточку, ни останавливать норматив опроса."""
    state = _call_state()
    monkeypatch.setattr(hub, "store", MemorySessionStore())
    monkeypatch.setitem(hub._sessions, state.session_id, state)
    state.on_event("call.incoming")
    state.on_event("call.answer")
    clock.advance(5)
    state.pause()
    kio_before = state.kio.model_dump()

    async def run():
        with hub.trainee(state.session_id) as queue:
            async with hub.operation(state.session_id):
                await call_ws._handle(state.session_id, state, event)
            return _drain(queue)

    events = asyncio.run(run())

    assert [type(item) for item in events] == [ErrorEvent]
    assert state.kio.model_dump() == kio_before
    interview = state.timers.timers[TimerCode.INTERVIEW]
    assert interview.paused and not interview.stopped
    assert TimerCode.DDS_ACK not in state.timers.timers
    assert TimerCode.CALLBACK not in state.timers.timers
    assert state.hints_shown == []


def test_station_channel_rejects_card_ack_while_paused(clock, monkeypatch):
    state, card = _dds_state_with_active_card(clock)
    monkeypatch.setattr(hub, "store", MemorySessionStore())
    monkeypatch.setitem(hub._sessions, state.session_id, state)
    clock.advance(5)
    state.pause()
    card_before = card.model_dump()

    async def run():
        with hub.station(state.session_id) as queue:
            async with hub.operation(state.session_id):
                await station_ws._handle(state.session_id, state, CardAck(comment="Принято"))
            return _drain(queue)

    events = asyncio.run(run())

    assert [type(item) for item in events] == [ErrorEvent]
    assert card.model_dump() == card_before
    ack = card.timers.timers[TimerCode.DDS_ACK]
    assert ack.paused and not ack.stopped
    assert ack.current_ms(clock.now) == 5_000


def test_repeated_pause_and_resume_are_idempotent(clock, monkeypatch):
    state, card = _dds_state_with_active_card(clock)
    store = MemorySessionStore()
    monkeypatch.setattr(hub, "store", store)
    monkeypatch.setitem(hub._sessions, state.session_id, state)
    base = now_utc()
    monkeypatch.setattr("app.session.state.now_utc", lambda: base)

    async def command(event):
        async with hub.operation(state.session_id):
            await control_ws._command(state.session_id, event, TEACHER)

    async def run():
        with hub.station(state.session_id) as queue:
            clock.advance(5)
            await command(SessionPause())
            clock.advance(30)
            await command(SessionPause())  # второе нажатие не сдвигает начало паузы
            monkeypatch.setattr("app.session.state.now_utc", lambda: base + timedelta(seconds=60))
            await command(SessionResume())
            await command(SessionResume())
            return _drain(queue)

    events = asyncio.run(run())
    clock.advance(10)

    records = [record for _sid, batch in store.commits for record in batch]
    assert sum(isinstance(record, LessonPaused) for record in records) == 1
    assert sum(isinstance(record, LessonResumed) for record in records) == 1
    assert [event.paused for event in events if isinstance(event, SessionPaused)] == [True, False]
    assert state.total_paused_ms == 60_000
    assert state.pause_intervals == [(base, base + timedelta(seconds=60))]
    assert card.timers.timers[TimerCode.DDS_ACK].current_ms(clock.now) == 15_000


def test_report_reaction_excludes_pause_after_control_and_checkpoint(clock, monkeypatch):
    base = datetime(2026, 9, 27, 10, tzinfo=UTC)
    state, card = _dds_state_with_active_card(clock)
    state.criteria.dds_report_reaction_enabled = True
    service = state.card_services(card)[0]
    monkeypatch.setattr("app.session.dds.now_utc", lambda: base)
    card.set_service_status(service, ServiceStatus.ACCEPTED, "Карточка принята")
    card.phone_reports.append(PhoneReportRecord(
        service=service, crew="Бригада 12", phase="dispatched",
        text="Бригада выехала", at=base,
    ))
    store = MemorySessionStore()
    monkeypatch.setattr(hub, "store", store)
    monkeypatch.setitem(hub._sessions, state.session_id, state)

    async def command(event):
        async with hub.operation(state.session_id):
            await control_ws._command(state.session_id, event, TEACHER)

    monkeypatch.setattr("app.session.state.now_utc", lambda: base + timedelta(seconds=10))
    asyncio.run(command(SessionPause()))
    restored = load_state(dump_state(state), base + timedelta(seconds=70))
    assert restored.paused and restored.pause_intervals == []
    monkeypatch.setitem(hub._sessions, state.session_id, restored)
    monkeypatch.setattr("app.session.state.now_utc", lambda: base + timedelta(seconds=70))
    asyncio.run(command(SessionResume()))
    monkeypatch.setattr("app.session.dds.now_utc", lambda: base + timedelta(seconds=80))
    restored.desk.active.set_service_status(
        service, ServiceStatus.RESPONDING, "Основание: доклад. Сведения: бригада выехала.",
    )

    result = score_dds_card(restored, restored.desk.active)
    reaction = next(item for item in result.metrics if item.key == "dds_report_reaction")
    assert restored.pause_intervals == [
        (base + timedelta(seconds=10), base + timedelta(seconds=70)),
    ]
    assert reaction.passed and "20 с" in reaction.fact
    assert not any(item.metric_key == "dds_report_reaction" for item in result.findings)


def test_pause_is_ignored_in_ended_session(clock, monkeypatch):
    state, _card = _dds_state_with_active_card(clock)
    store = MemorySessionStore()
    monkeypatch.setattr(hub, "store", store)
    monkeypatch.setitem(hub._sessions, state.session_id, state)
    state.end(CallEndReason.INSTRUCTOR)

    async def run():
        with hub.station(state.session_id) as queue:
            async with hub.operation(state.session_id):
                await control_ws._command(state.session_id, SessionPause(), TEACHER)
            return _drain(queue)

    events = asyncio.run(run())

    assert state.paused is False
    assert state.paused_at is None
    assert events == []
    assert not any(isinstance(record, LessonPaused) for _sid, batch in store.commits for record in batch)
