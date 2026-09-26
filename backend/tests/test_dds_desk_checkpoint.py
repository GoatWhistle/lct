"""Чекпойнт пульта ДДС: занятие продолжается с той же активной карточкой,
чтения ничего не меняют.

Состояния собраны в коде и проходят через снимок: пульт ДДС на три карточки,
упражнение 112 с живым диспетчером и связка 112→ДДС.
"""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from app.api.ws import station as station_ws
from app.domain.events import Exercise, SessionMode, StationToServer
from app.domain.kio import KIO
from app.domain.statuses import ServiceStatus
from app.domain.timers import TimerCode
from app.scenarios.loader import load_file
from app.session import finish as finish_module
from app.session import timers as timers_module
from app.session.checkpoint import dump_state, load_state
from app.session.dds import prepare_handoff_queue, prepare_queue
from app.session.hub import hub
from app.session.state import SessionState
from app.session.timers import now_utc

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"
_events = TypeAdapter(StationToServer)


@pytest.fixture(autouse=True)
def offline_finish(monkeypatch):
    class NoCoaching:
        def model_dump(self, **_kwargs):
            return {}

    async def no_coach(_metrics):
        return NoCoaching()

    monkeypatch.setattr(finish_module, "coach", no_coach)
    monkeypatch.setattr(hub, "journal", None)


def fire():
    return load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)


def accept_with_crew(state: SessionState, card) -> None:
    service = state.card_services(card)[0]
    card.set_service_status(service, ServiceStatus.ACCEPTED, "Принято в работу")
    card.on_event("card.ack")
    card.crew_selected = state.crew_options(card)[0]
    card.crew_assignments[service] = card.crew_selected
    card.dds_log.append(("crew.select", now_utc(), card.crew_selected))


def dds_state() -> SessionState:
    first = fire()
    scenarios = [first.model_copy(deep=True, update={"id": f"desk-{index}"})
                 for index in range(3)]
    state = SessionState(
        session_id=uuid4(), scenario_id=first.id, scenario_title=first.title,
        level=first.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
    )
    prepare_queue(state, scenarios)
    first_card, second = state.desk.ordered()[:2]
    accept_with_crew(state, first_card)
    state.desk.open(second.card_id)
    second.reply_text = "Ответ по второй карточке"
    return state


def call_state() -> SessionState:
    scenario = fire()
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING, exercise=Exercise.CALL,
        scenario=scenario.model_copy(deep=True),
        required_fields=list(scenario.required_fields),
    )
    state.on_event("call.incoming")
    state.on_event("call.answer")
    state.kio = KIO(address="улица Ленина, 14", notify=["Служба 101"])
    state.dispatch()
    state.on_event("dds.dispatch")
    accept_with_crew(state, state.desk.active)
    return state


def handoff_state() -> SessionState:
    first = fire()
    second = load_file(LIBRARY / "tickets" / "t01-1-fire-container.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=first.id, scenario_title=first.title,
        level=first.level.value, mode=SessionMode.TRAINING,
        exercise=Exercise.CARD, handoff_to_dds=True, scenario=first,
    )
    state.kio = KIO(address="улица Ленина, 14", description="горит балкон",
                    notify=["Служба 101"])
    state.dispatch()
    prepare_handoff_queue(state, [second])
    accept_with_crew(state, state.desk.active)
    return state


BUILDERS = {"dds": dds_state, "call": call_state, "handoff": handoff_state}


def restored(name: str) -> SessionState:
    return load_state(dump_state(BUILDERS[name]()), now_utc())


def station(state, **payload) -> None:
    hub.register(state)
    asyncio.run(station_ws._handle(state.session_id, state, _events.validate_python(payload)))


def test_dds_checkpoint_resumes_same_active_card_and_work():
    before = dds_state()
    state = load_state(dump_state(before), now_utc())

    assert state.desk.active_id == before.desk.active_id
    assert state.desk.active.original_index == 1
    assert state.desk.active.reply_text == "Ответ по второй карточке"
    first = state.desk.ordered()[0]
    assert first.status_log[-1].status is ServiceStatus.ACCEPTED
    assert first.crew_selected == before.desk.ordered()[0].crew_selected
    assert first.timers.measured_ms(TimerCode.DDS_ACK) is not None

    station(state, type="card.next", card_id=str(before.desk.active_id))

    assert [record.card_id for record in state.desk.completed] == [before.desk.active_id]
    assert state.desk.active.original_index == 2
    assert state.station_snapshot().card_index == 3


def test_call_checkpoint_keeps_dispatcher_work_on_shared_timers():
    before = call_state()
    state = load_state(dump_state(before), now_utc())

    card = state.desk.active
    assert card.card_id == state.dispatched_card.card_id
    assert card.crew_selected == before.desk.active.crew_selected
    assert [mark.status for mark in card.status_log] == [ServiceStatus.ACCEPTED]
    assert card.timers is state.timers
    snapshot = state.station_snapshot()
    assert snapshot.statuses == {snapshot.managed_service: ServiceStatus.ACCEPTED}
    assert snapshot.queue_cards == []

    state.ended_at = now_utc()
    asyncio.run(finish_module.finish(state.session_id, state))

    crew = next(item for item in state.score["metrics"] if item["key"] == "dds_crew")
    assert crew["passed"] and crew["fact"] == card.crew_selected
    assert any(item["key"] == "interview_time" for item in state.score["metrics"])


def test_handoff_checkpoint_keeps_operator_card_and_queue():
    state = restored("handoff")

    assert state.operator_kio.address == "улица Ленина, 14"
    assert [item.id for item in state.desk.scenarios] == [
        "fire-apartment-l2", "t01-1-fire-container"]
    assert state.desk.active.original_index == 0
    assert state.desk.active.status_log[-1].status is ServiceStatus.ACCEPTED


@pytest.mark.parametrize("name", ["dds", "call", "handoff"])
def test_reads_do_not_change_state(name, monkeypatch):
    monkeypatch.setattr(timers_module.time, "monotonic", lambda: 1_000_000.0)
    state = restored(name)
    active = state.desk.active

    first = dump_state(state)
    state.station_snapshot()
    state.snapshot()
    state.card_received_event()

    assert dump_state(state) == first
    assert state.desk.active is active


@pytest.mark.parametrize("name", ["dds", "handoff"])
def test_finish_scores_every_card_without_switching_active(name):
    state = restored(name)
    active = state.desk.active
    unscored = {card.card_id for card in state.desk.cards.values()}

    state.ended_at = now_utc()
    asyncio.run(finish_module.finish(state.session_id, state))

    assert state.desk.active is active
    assert {record.card_id for record in state.desk.completed} == unscored


def test_handoff_keeps_operator_timers_for_card_fill_metric():
    """До пульта связка подменяла таймеры оператора таймерами карточки ДДС,
    и время заполнения карточки 112 в оценку не попадало."""
    first = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=first.id, scenario_title=first.title,
        level=first.level.value, mode=SessionMode.TRAINING,
        exercise=Exercise.CARD, handoff_to_dds=True, scenario=first,
    )
    state.on_event("card.start")
    state.kio = KIO(address="улица Ленина, 14", notify=["Служба 101"])
    state.on_event("card.submit")
    state.dispatch()
    prepare_handoff_queue(state, [])

    assert state.timers.measured_ms(TimerCode.CARD_FILL) is not None
    assert state.desk.active.timers is not state.timers
    assert load_state(dump_state(state), now_utc()).timers.measured_ms(
        TimerCode.CARD_FILL) is not None
