"""Пауза занятия с пульта преподавателя (lct-39).

Часы — управляемый монотонный счётчик: тест продвигает их сам, а не спит,
проверяя ровно то, что требует карточка — пауза не считается в норматив,
переживает чекпойнт и пишется в аудит.
"""

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.api.ws import control as control_ws
from app.domain.events import Exercise, LessonCriteria, SessionMode
from app.domain.timers import TimerCode
from app.scenarios.loader import load_file
from app.session import timers as timers_module
from app.session.checkpoint import dump_state, load_state
from app.session.hub import hub
from app.session.state import SessionState, now_utc
from app.session.store import LessonPaused, LessonResumed, MemorySessionStore
from app.session.timers import Timer

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


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
