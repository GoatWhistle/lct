"""Оценки finish() по пульту ДДС закреплены эталоном (lct-dds-desk).

Эталон `golden/dds_desk_scores.json` снят с кода до переноса живых карточек
в `DdsDesk`: перенос меняет владельца состояния, а не правила оценки.
Одно отличие намеренное: в связке 112→ДДС `card_fill_time` теперь считается
по таймерам оператора — до пульта их подменяли таймеры карточки ДДС.
Пересобрать эталон — `UPDATE_GOLDEN=1 pytest tests/test_dds_desk_scores.py`,
только если правила оценки меняются намеренно.
"""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from app.api.ws import station as station_ws
from app.domain.events import Exercise, SessionMode, StationToServer
from app.domain.kio import KIO
from app.scenarios.loader import load_file
from app.session import finish as finish_module
from app.session.dds import prepare_handoff_queue, prepare_queue
from app.session.hub import hub
from app.session.store import MemorySessionStore
from app.session.state import SessionState

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"
GOLDEN = Path(__file__).parent / "golden" / "dds_desk_scores.json"
COMMENT = "Основание: доклад старшего.\nСведения: карточка принята, адресат — дежурный."
_events = TypeAdapter(StationToServer)


async def finish_in_operation(state):
    """finish() пишет оценку в commit операции, завершившей занятие."""
    async with hub.operation(state.session_id):
        await finish_module.finish(state.session_id, state)


@pytest.fixture(autouse=True)
def offline_finish(monkeypatch):
    class NoCoaching:
        def model_dump(self, **_kwargs):
            return {}

    async def no_coach(_metrics):
        return NoCoaching()

    monkeypatch.setattr(finish_module, "coach", no_coach)
    monkeypatch.setattr(hub, "store", MemorySessionStore())


def scenario(name: str):
    return load_file(LIBRARY / name, LIBRARY)


def station(state: SessionState, **payload) -> None:
    hub.register(state)
    event = _events.validate_python(payload)

    async def command():
        async with hub.operation(state.session_id):
            await station_ws._handle(state.session_id, state, event)

    asyncio.run(command())


def active_id(state: SessionState):
    return state.station_snapshot().card_id


def service_of(state: SessionState) -> str:
    return state.station_snapshot().managed_service


def work_card(state: SessionState) -> None:
    """Принять, назначить бригаду и довести до выезда — типичный путь курсанта."""
    station(state, type="card.ack", comment=COMMENT)
    snapshot = state.station_snapshot()
    station(state, type="crew.select", crew=snapshot.crew_options[0])
    station(state, type="card.status", service=service_of(state),
            status="responding", comment="Бригада выехала, доклад дежурному")
    station(state, type="zone.decision", in_zone=True)


def summarize(state: SessionState) -> dict:
    score = state.score
    return {
        "score_auto": score["score_auto"],
        "card_status": score["card_status"],
        "metrics": [
            [m["key"], m["title"], m["fact"], m["passed"], m["weight"], m.get("credit")]
            for m in score["metrics"]
        ],
        "findings": [[f["code"], f["summary"], f["fact"]] for f in score["findings"]],
        "cards": [[card["scenario_id"], card["score_auto"], card["reply_text"],
                   [action["type"] for action in card["actions"]]]
                  for card in score["card_results"]],
    }


def dds_exercise_several_cards() -> SessionState:
    cards = [scenario("fire-apartment-l2.yaml"),
             scenario("tickets/t20-2-stroke.yaml"),
             scenario("tickets/t01-1-fire-container.yaml")]
    state = SessionState(
        session_id=uuid4(), scenario_id=cards[0].id, scenario_title=cards[0].title,
        level=cards[0].level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
    )
    prepare_queue(state, cards)
    first = active_id(state)
    work_card(state)
    station(state, type="card.reply", card_id=str(first), text="Принято, бригада направлена")
    station(state, type="card.next", card_id=str(first))
    second = active_id(state)
    station(state, type="card.ack", comment=COMMENT)
    third = next(card.card_id for card in state.station_snapshot().queue_cards
                 if card.card_id not in {first, second})
    station(state, type="card.open", card_id=str(third))
    work_card(state)
    station(state, type="card.open", card_id=str(second))
    station(state, type="station.finish")
    return state


def dds_exercise_early_finish() -> SessionState:
    cards = [scenario("tickets/t20-2-stroke.yaml"),
             scenario("fire-apartment-l2.yaml"),
             scenario("tickets/t01-1-fire-container.yaml")]
    state = SessionState(
        session_id=uuid4(), scenario_id=cards[0].id, scenario_title=cards[0].title,
        level=cards[0].level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
    )
    prepare_queue(state, cards, max_waiting=1)
    station(state, type="card.ack", comment=COMMENT)
    station(state, type="station.finish")
    return state


def handoff_112_to_dds() -> SessionState:
    first = scenario("fire-apartment-l2.yaml")
    second = scenario("tickets/t01-1-fire-container.yaml")
    state = SessionState(
        session_id=uuid4(), scenario_id=first.id, scenario_title=first.title,
        level=first.level.value, mode=SessionMode.TRAINING,
        exercise=Exercise.CARD, handoff_to_dds=True, scenario=first,
        required_fields=list(first.required_fields),
        pending_dds_scenarios=[second],
    )
    state.on_event("card.start")
    state.kio = KIO(
        address="улица Ленина, 14", description="горит балкон на третьем этаже",
        notify=["Служба 101"],
    )
    state.on_event("card.submit")
    state.dispatch()
    prepare_handoff_queue(state, state.pending_dds_scenarios)
    first_card = active_id(state)
    work_card(state)
    station(state, type="card.next", card_id=str(first_card))
    station(state, type="card.ack", comment=COMMENT)
    station(state, type="station.finish")
    return state


def call_with_live_dispatcher() -> SessionState:
    first = scenario("fire-apartment-l2.yaml")
    state = SessionState(
        session_id=uuid4(), scenario_id=first.id, scenario_title=first.title,
        level=first.level.value, mode=SessionMode.TRAINING, exercise=Exercise.CALL,
        scenario=first, required_fields=list(first.required_fields),
    )
    state.on_event("call.incoming")
    state.on_event("call.answer")
    state.patch_kio({"address": "улица Ленина, 14", "description": "горит квартира",
                     "signs": list(first.signs)})
    state.on_event("dds.dispatch")
    state.dispatch()
    work_card(state)
    state.ended_at = state.dispatched_at
    asyncio.run(finish_in_operation(state))
    return state


CASES = {
    "dds_several_cards": dds_exercise_several_cards,
    "dds_early_finish": dds_exercise_early_finish,
    "handoff_112_to_dds": handoff_112_to_dds,
    "call_live_dispatcher": call_with_live_dispatcher,
}


def test_update_golden_when_requested():
    if not os.environ.get("UPDATE_GOLDEN"):
        pytest.skip("эталон пересобирается только явно")
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(json.dumps({name: summarize(build()) for name, build in CASES.items()},
                                 ensure_ascii=False, indent=1) + "\n")


@pytest.mark.parametrize("name", list(CASES))
def test_finish_scores_match_golden(name):
    state = CASES[name]()

    assert summarize(state) == json.loads(GOLDEN.read_text())[name]
