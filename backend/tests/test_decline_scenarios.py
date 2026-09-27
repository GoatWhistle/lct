"""Сценарии с эталонным отказом ДДС (lct-52).

Мотивированный отказ по эталону не даёт отметок; «Принята» вопреки эталону —
D2 с причиной из сценария в норме. Причины — из памятки АРМ-112: не наша
зона, не наша компетенция, дубль.
"""

from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.events import Exercise, SessionMode
from app.scenarios.loader import load_file
from app.session import finish as finish_module
from app.session.dds import prepare_queue
from app.session.hub import hub
from app.session.state import SessionState
from app.session.store import MemorySessionStore

from tests.test_dds_desk_scores import station

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"
DECLINES = ["dds-decline-duplicate.yaml", "dds-decline-out-of-zone.yaml"]


@pytest.fixture(autouse=True)
def offline_finish(monkeypatch):
    class NoCoaching:
        def model_dump(self, **_kwargs):
            return {}

    async def no_coach(_metrics):
        return NoCoaching()

    monkeypatch.setattr(finish_module, "coach", no_coach)
    monkeypatch.setattr(hub, "store", MemorySessionStore())


def lesson(name: str) -> SessionState:
    scenario = load_file(LIBRARY / name, LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
    )
    prepare_queue(state, [scenario])
    station(state, type="card.open", card_id=str(state.station_snapshot().card_id))
    return state


@pytest.mark.parametrize("name", DECLINES)
def test_library_scenario_expects_a_reasoned_decline(name):
    scenario = load_file(LIBRARY / name, LIBRARY)

    assert scenario.dds_decision.expected == "decline"
    assert scenario.dds_decision.reason.split(":")[0] in {"дубль", "не наша зона"}


@pytest.mark.parametrize("name", DECLINES)
def test_reasoned_decline_by_the_reference_leaves_a_clean_report(name):
    state = lesson(name)
    service = state.station_snapshot().managed_service
    reason = load_file(LIBRARY / name, LIBRARY).dds_decision.reason

    station(state, type="card.status", service=service, status="declined",
            comment=f"Основание: {reason}.\nСведения: причина отказа передана дежурному 112.")
    station(state, type="station.finish")

    score = state.score
    assert score["findings"] == []
    decision = next(item for item in score["metrics"] if item["key"] == "dds_decision")
    assert decision["passed"] and decision["norm"] == reason
    assert score["score_auto"] == 100.0


@pytest.mark.parametrize("name", DECLINES)
def test_accepting_against_the_reference_is_d2_with_the_scenario_reason(name):
    state = lesson(name)
    reason = load_file(LIBRARY / name, LIBRARY).dds_decision.reason

    station(state, type="card.ack",
            comment="Основание: карточка поступила.\nСведения: принята, доклад дежурному.")
    station(state, type="station.finish")

    score = state.score
    d2 = [item for item in score["findings"]
          if item["code"] == "D2" and "вопреки эталону" in item["summary"]]
    assert len(d2) == 1 and d2[0]["norm"] == reason
    decision = next(item for item in score["metrics"] if item["key"] == "dds_decision")
    assert not decision["passed"]
