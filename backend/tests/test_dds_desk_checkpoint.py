"""Чекпойнт пульта ДДС: формат прежний, чтения ничего не меняют.

`golden/checkpoint_before_desk.json` записан кодом до `DdsDesk`: занятие,
поднятое из такого снимка, продолжается с той же активной карточкой.
"""

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter

from app.api.ws import station as station_ws
from app.domain.events import Exercise, SessionMode, StationToServer
from app.domain.kio import KIO
from app.domain.statuses import ServiceStatus
from app.domain.timers import TimerCode
from app.scenarios.loader import load_file
from app.session import checkpoint
from app.session import finish as finish_module
from app.session.checkpoint import dump_state, load_state
from app.session.dds import prepare_handoff_queue
from app.session.hub import hub
from app.session.state import SessionState
from app.session.timers import now_utc

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"

BEFORE_DESK = json.loads(
    (Path(__file__).parent / "golden" / "checkpoint_before_desk.json").read_text()
)
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


def restored(name: str, **changes):
    payload = {**json.loads(json.dumps(BEFORE_DESK[name])), **changes}
    return load_state(payload, now_utc())


def station(state, **payload) -> None:
    hub.register(state)
    asyncio.run(station_ws._handle(state.session_id, state, _events.validate_python(payload)))


def test_dds_checkpoint_before_desk_resumes_same_active_card_and_work():
    state = restored("dds")
    saved = BEFORE_DESK["dds"]

    assert str(state.desk.active_id) == saved["dds_active_card_id"]
    assert state.desk.active.original_index == 1
    assert state.desk.active.reply_text == "Ответ по второй карточке"
    first = state.desk.ordered()[0]
    assert first.status_log[-1].status is ServiceStatus.ACCEPTED
    assert first.crew_selected == saved["dds_live_cards"][0]["crew_selected"]
    assert first.timers.measured_ms(TimerCode.DDS_ACK) is not None

    station(state, type="card.next", card_id=saved["dds_active_card_id"])

    assert [record.card_id for record in state.desk.completed] == [
        UUID(saved["dds_active_card_id"])]
    assert state.desk.active.original_index == 2
    assert state.station_snapshot().card_index == 3


def test_legacy_checkpoint_without_active_id_opens_first_card():
    payload = json.loads(json.dumps(BEFORE_DESK["dds"]))
    del payload["dds_active_card_id"]

    state = load_state(payload, now_utc())

    assert state.desk.active.original_index == 0
    assert state.station_snapshot().card_index == 1


def test_call_checkpoint_before_desk_moves_dispatcher_work_to_desk_card():
    state = restored("call")
    saved = BEFORE_DESK["call"]

    card = state.desk.active
    assert card.card_id == state.dispatched_card.card_id
    assert card.crew_selected == saved["crew_selected"]
    assert [mark.status for mark in card.status_log] == [ServiceStatus.ACCEPTED]
    assert card.timers is state.timers
    snapshot = state.station_snapshot()
    assert snapshot.statuses == {snapshot.managed_service: ServiceStatus.ACCEPTED}
    assert snapshot.queue_cards == []

    state.ended_at = now_utc()
    asyncio.run(finish_module.finish(state.session_id, state))

    crew = next(item for item in state.score["metrics"] if item["key"] == "dds_crew")
    assert crew["passed"] and crew["fact"] == saved["crew_selected"]
    assert any(item["key"] == "interview_time" for item in state.score["metrics"])


def test_handoff_checkpoint_before_desk_keeps_operator_card_and_queue():
    state = restored("handoff")

    assert state.operator_kio.address == "улица Ленина, 14"
    assert [item.id for item in state.desk.scenarios] == [
        "fire-apartment-l2", "t01-1-fire-container"]
    assert state.desk.active.original_index == 0
    assert state.desk.active.status_log[-1].status is ServiceStatus.ACCEPTED


@pytest.mark.parametrize("name", ["dds", "call", "handoff"])
def test_new_checkpoint_keeps_json_keys_of_format_before_desk(name):
    assert set(dump_state(restored(name))) == set(BEFORE_DESK[name])


@pytest.mark.parametrize("name", ["dds", "call", "handoff"])
def test_reads_do_not_change_state(name, monkeypatch):
    monkeypatch.setattr(checkpoint.time, "monotonic", lambda: 1_000_000.0)
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
