"""Пауза занятия с пульта преподавателя (lct-39).

Часы — управляемый монотонный счётчик: тест продвигает их сам, а не спит,
проверяя ровно то, что требует карточка — пауза не считается в норматив,
переживает чекпойнт и пишется в аудит.
"""

import asyncio
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.http import sessions as sessions_http
from app.api.ws import control as control_ws
from app.domain.events import CallEndReason, Exercise, LessonCriteria, SessionMode
from app.domain.statuses import ServiceStatus
from app.domain.timers import TimerCode
from app.main import app
from app.scenarios.loader import load_file
from app.scoring.dispatcher import dispatcher_metrics
from app.session import timers as timers_module
from app.session.checkpoint import dump_state, load_state
from app.session.finish import end_session
from app.session.hub import hub
from app.session.state import SessionState, now_utc
from app.session.store import LessonPaused, LessonResumed, MemorySessionStore
from app.session.timers import Timer

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


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
    отметка приходит на 25-й секунде активного времени, а не 85-й реальной."""
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
    card.on_event("card.ack")

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
    who = type("Who", (), {"login": "teacher", "role": type("R", (), {"value": "instructor"})()})()

    async def run():
        async with hub.operation(state.session_id):
            await control_ws._command(
                state.session_id, type("E", (), {"type": "session.pause"})(), who,
            )
        async with hub.operation(state.session_id):
            await control_ws._command(
                state.session_id, type("E", (), {"type": "session.resume"})(), who,
            )

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
    card.set_service_status(service, ServiceStatus.ACCEPTED, "Принято")
    card.on_event("card.ack")

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

    async def run():
        async with hub.operation(state.session_id):
            await end_session(state.session_id, state, CallEndReason.INSTRUCTOR)

    asyncio.run(run())

    assert state.ended
    assert state.paused is False
    assert state.total_paused_ms >= 30_000


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
