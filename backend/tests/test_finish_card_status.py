"""card_status отчёта — статус карточки, активной у курсанта при завершении."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from app.domain.events import Exercise, SessionMode
from app.domain.kio import KIO
from app.domain.statuses import CardStatus, ServiceStatus
from app.scenarios.loader import load_file
from app.session import finish as finish_module
from app.session.dds import prepare_handoff_queue, prepare_queue
from app.session.hub import hub
from app.session.store import MemorySessionStore
from app.session.state import SessionState

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


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


def accept_active_card(state: SessionState) -> None:
    card = state.desk.active
    service = card.dispatched_card.notify[0]
    card.set_service_status(
        service, ServiceStatus.ACCEPTED,
        "Основание: доклад старшего.\nСведения: карточка принята.",
    )


def assert_active_card_kept(state: SessionState, active_id, active_status) -> None:
    assert active_status is not CardStatus.REGISTERED
    assert state.score["card_status"] == active_status.value
    assert state.desk.active_id == active_id
    assert len(state.score["card_results"]) == 2


def test_dds_finish_reports_status_of_active_card_not_last_scored():
    first = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    second = load_file(LIBRARY / "tickets" / "t20-2-stroke.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=first.id, scenario_title=first.title,
        level=first.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
    )
    prepare_queue(state, [first, second])
    assert len(state.desk.cards) == 2
    active_id = state.desk.active_id
    accept_active_card(state)
    active_status = state.station_snapshot().card

    asyncio.run(finish_in_operation(state))

    assert_active_card_kept(state, active_id, active_status)


def test_handoff_finish_reports_status_of_active_card_not_last_scored():
    first = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    second = load_file(LIBRARY / "tickets" / "t01-1-fire-container.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=first.id, scenario_title=first.title,
        level=first.level.value, mode=SessionMode.TRAINING,
        exercise=Exercise.CARD, handoff_to_dds=True, scenario=first,
        pending_dds_scenarios=[second],
    )
    state.kio = KIO(
        address="улица Ленина, 14", description="горит балкон", notify=["Служба 101"],
    )
    state.dispatch()
    prepare_handoff_queue(state, state.pending_dds_scenarios)
    assert len(state.desk.cards) == 2
    active_id = state.desk.active_id
    accept_active_card(state)
    active_status = state.station_snapshot().card

    asyncio.run(finish_in_operation(state))

    assert_active_card_kept(state, active_id, active_status)
