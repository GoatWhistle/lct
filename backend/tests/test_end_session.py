"""Завершение занятия — одна операция `end_session`.

Порядок шагов проверяется без websocket: фейковый хаб пишет журнал вызовов,
фейковый голос и фейковая оценка отмечают себя в том же журнале.
"""

import asyncio
import time
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.events import CallEndReason, Exercise, SessionMode
from app.domain.kio import KIO
from app.domain.timers import TimerCode
from app.scenarios.loader import load_file
from app.session import finish as finish_module
from app.session.dds import prepare_handoff_queue, prepare_queue
from app.session.hub import hub
from app.session.state import SessionState
from app.session.store import LessonEnded, MemorySessionStore

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


def fire():
    return load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)


def dds_state() -> SessionState:
    """Пульт ДДС на две карточки, вторая открыта: `DDS_WORK` идёт."""
    first = fire()
    scenarios = [first.model_copy(deep=True, update={"id": f"desk-{index}"})
                 for index in range(2)]
    state = SessionState(
        session_id=uuid4(), scenario_id=first.id, scenario_title=first.title,
        level=first.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
    )
    prepare_queue(state, scenarios)
    card = state.desk.ordered()[1]
    state.desk.open(card.card_id)
    # Карточка принята: три минуты отработки идут с этого момента.
    card.on_event("card.ack")
    card.on_event("dds.open")
    return state


def card_state(*, handoff: bool = False) -> SessionState:
    scenario = fire()
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING,
        exercise=Exercise.CARD, handoff_to_dds=handoff, scenario=scenario,
    )
    state.kio = KIO(address="улица Ленина, 14", notify=["Служба 101"])
    if handoff:
        state.dispatch()
        prepare_handoff_queue(state, [])
    return state


def work_stopped(state) -> bool:
    return all(
        card.timers.measured_ms(TimerCode.DDS_WORK) is not None
        for card in state.desk.cards.values()
        if TimerCode.DDS_WORK in card.timers.timers
    )


class FakeHub:
    def __init__(self, log: list, state) -> None:
        self.log = log
        self.state = state

    def stop_ticker(self, _session_id) -> None:
        self.log.append(("ticker", work_stopped(self.state)))

    def record(self, _session_id, record) -> None:
        self.log.append(("record", type(record).__name__))

    def to_station(self, _session_id, event) -> None:
        self.log.append(("station", event.type))

    def to_trainee(self, _session_id, event) -> None:
        self.log.append(("trainee", event.type))

    def to_observers(self, _session_id, event) -> None:
        self.log.append(("observers", event.type))


class FakeVoice:
    def __init__(self, log: list, state) -> None:
        self.log = log
        self.state = state

    async def close(self) -> None:
        self.log.append(("voice", work_stopped(self.state)))


@pytest.fixture
def fake(monkeypatch):
    def install(state):
        log: list = []
        monkeypatch.setattr(finish_module, "hub", FakeHub(log, state))

        async def fake_finish(_session_id, finished):
            log.append(("finish", finished.ended))
            finished.score = {"score": 1.0}

        monkeypatch.setattr(finish_module, "finish", fake_finish)
        return log
    return install


def test_end_session_runs_steps_in_fixed_order(fake):
    state = dds_state()
    log = fake(state)
    state.voice = FakeVoice(log, state)

    asyncio.run(finish_module.end_session(state.session_id, state, CallEndReason.INSTRUCTOR))

    assert state.ended and state.end_reason is CallEndReason.INSTRUCTOR
    # таймеры → голос → тикер → журнал → оценка → рассылка
    assert log == [
        ("voice", True),
        ("ticker", True),
        ("record", "LessonEnded"),
        ("finish", True),
        ("observers", "session.ended"),
        ("station", "session.ended"),
        ("observers", "score.ready"),
        ("station", "score.ready"),
    ]


def test_card_without_handoff_tells_trainee_and_releases_score(fake):
    state = card_state()
    log = fake(state)

    asyncio.run(finish_module.end_session(state.session_id, state, CallEndReason.COMPLETE))

    events = [entry for entry in log if entry[0] in {"observers", "station", "trainee"}]
    assert events == [
        ("observers", "session.ended"),
        ("trainee", "call.ended"),
        ("observers", "score.ready"),
        ("trainee", "score.ready"),
    ]


def test_card_handed_to_dds_ends_on_station(fake):
    state = card_state(handoff=True)
    log = fake(state)

    asyncio.run(finish_module.end_session(state.session_id, state, CallEndReason.INSTRUCTOR))

    events = [entry for entry in log if entry[0] in {"observers", "station", "trainee"}]
    assert events == [
        ("observers", "session.ended"),
        ("station", "session.ended"),
        ("observers", "score.ready"),
        ("trainee", "score.ready"),
        ("station", "score.ready"),
    ]


def test_card_stopped_before_submit_stops_fill_timer(fake):
    state = card_state()
    state.on_event("card.start")
    fake(state)

    asyncio.run(finish_module.end_session(state.session_id, state, CallEndReason.INSTRUCTOR))

    assert state.timers.measured_ms(TimerCode.CARD_FILL) is not None


def test_second_end_session_writes_and_sends_nothing(fake):
    state = dds_state()
    log = fake(state)
    asyncio.run(finish_module.end_session(state.session_id, state, CallEndReason.COMPLETE))
    ended_at = state.ended_at
    log.clear()

    asyncio.run(finish_module.end_session(state.session_id, state, CallEndReason.INSTRUCTOR))

    assert log == []
    assert state.ended_at == ended_at
    assert state.end_reason is CallEndReason.COMPLETE


@pytest.fixture
def frozen_clock(monkeypatch):
    clock = {"now": time.monotonic()}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    class NoCoaching:
        def model_dump(self, **_kwargs):
            return {}

    async def no_coach(_metrics):
        return NoCoaching()

    monkeypatch.setattr(finish_module, "coach", no_coach)
    monkeypatch.setattr(hub, "store", MemorySessionStore())
    return clock


def test_instructor_stop_in_dds_stops_work_timer_and_keeps_score(frozen_clock):
    before, after = dds_state(), dds_state()
    frozen_clock["now"] += 95

    async def old_stop(state):
        # Прежний `_stop`: оценка брала время отработки по `current_ms()`.
        hub.register(state)
        async with hub.operation(state.session_id):
            ended_at = state.end(CallEndReason.INSTRUCTOR)
            hub.record(state.session_id, LessonEnded(ended_at, CallEndReason.INSTRUCTOR.value))
            await finish_module.finish(state.session_id, state)

    async def new_stop(state):
        hub.register(state)
        async with hub.operation(state.session_id):
            await finish_module.end_session(state.session_id, state, CallEndReason.INSTRUCTOR)

    asyncio.run(old_stop(before))
    asyncio.run(new_stop(after))

    active = after.desk.cards[after.desk.active_id]
    assert active.timers.measured_ms(TimerCode.DDS_WORK) == 95_000
    assert after.score["score_auto"] == before.score["score_auto"]
    assert after.score["metrics"] == before.score["metrics"]
    assert after.score["findings"] == before.score["findings"]
