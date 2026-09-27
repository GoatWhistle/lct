"""Оценка занятия по снимку: без websocket, без живого `state` внутри расчёта."""

import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.events import Exercise, LessonCriteria, SessionMode
from app.domain.kio import KIO
from app.domain.statuses import ServiceStatus
from app.domain.taxonomy import ErrorCode, FindingSource
from app.scenarios.loader import load_file
from app.scoring.grammar import GrammarAssessment
from app.session import finish as finish_module
from app.session.dds import prepare_handoff_queue, prepare_queue
from app.session.hub import hub
from app.session.score import ScoringSnapshot, score_session
from app.session.state import SessionState
from app.session.store import MemorySessionStore

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


def scenario(name: str):
    return load_file(LIBRARY / name, LIBRARY)


def card_state(*, grammar: bool = True, handoff: bool = False) -> SessionState:
    operator = scenario("fire-apartment-l2.yaml")
    state = SessionState(
        session_id=uuid4(), scenario_id=operator.id, scenario_title=operator.title,
        level=operator.level.value, mode=SessionMode.TRAINING, exercise=Exercise.CARD,
        handoff_to_dds=handoff, scenario=operator,
        criteria=LessonCriteria(require_correct_grammar=grammar),
    )
    state.kio = KIO(
        address="улица Ленина, 14", incident_type="fire", victims_count=2,
        description="горит балкон", notify=["Служба 101"],
    )
    state.on_event("card.start")
    state.dispatch()
    state.on_event("card.submit")
    if handoff:
        prepare_handoff_queue(state, [scenario("tickets/t01-1-fire-container.yaml")])
    return state


def dds_state() -> SessionState:
    first = scenario("fire-apartment-l2.yaml")
    state = SessionState(
        session_id=uuid4(), scenario_id=first.id, scenario_title=first.title,
        level=first.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
        criteria=LessonCriteria(require_correct_grammar=True),
    )
    prepare_queue(state, [first, scenario("tickets/t20-2-stroke.yaml")])
    return state


def grammar_says(passed: bool, calls: list[str]):
    async def check(text: str) -> GrammarAssessment:
        calls.append(text)
        return GrammarAssessment(
            passed=passed, errors=() if passed else ("нет завершающего знака препинания",),
            source="test",
        )
    return check


def score(snapshot, grammar):
    return asyncio.run(score_session(snapshot, grammar=grammar))


@pytest.fixture(autouse=True)
def offline_finish(monkeypatch):
    class NoCoaching:
        def model_dump(self, **_kwargs):
            return {}

    async def no_coach(_metrics):
        return NoCoaching()

    monkeypatch.setattr(finish_module, "coach", no_coach)
    monkeypatch.setattr(hub, "store", MemorySessionStore())


def test_passed_grammar_check_adds_passed_metric_without_e4():
    calls: list[str] = []
    snapshot = ScoringSnapshot.of(card_state())

    report = score(snapshot, grammar_says(True, calls))

    assert calls == ["горит балкон"]
    metric = next(item for item in report.result.metrics if item.key == "description_grammar")
    assert metric.passed and metric.fact == "ошибок не обнаружено"
    assert ErrorCode.E4 not in [item.code for item in report.result.findings]


def test_failed_grammar_check_adds_e4_and_lowers_score():
    passed = score(ScoringSnapshot.of(card_state()), grammar_says(True, []))
    failed = score(ScoringSnapshot.of(card_state()), grammar_says(False, []))

    metric = next(item for item in failed.result.metrics if item.key == "description_grammar")
    assert not metric.passed
    assert metric.fact == "нет завершающего знака препинания"
    e4 = next(item for item in failed.result.findings if item.code is ErrorCode.E4)
    assert e4.source is FindingSource.GRAMMAR
    assert failed.result.score < passed.result.score


def test_grammar_is_not_checked_without_criterion_or_on_dds_exercise():
    calls: list[str] = []

    off = score(ScoringSnapshot.of(card_state(grammar=False)), grammar_says(False, calls))
    dds = score(ScoringSnapshot.of(dds_state()), grammar_says(False, calls))

    assert calls == []
    assert "description_grammar" not in [item.key for item in off.result.metrics]
    assert "description_grammar" not in [item.key for item in dds.result.metrics]


def test_snapshot_is_read_only():
    snapshot = ScoringSnapshot.of(card_state())

    with pytest.raises(FrozenInstanceError):
        snapshot.scenario = None


def test_card_fill_time_is_scored_from_snapshot_timers():
    report = score(ScoringSnapshot.of(card_state(grammar=False)), grammar_says(True, []))

    metric = next(item for item in report.result.metrics if item.key == "card_fill_time")
    assert metric.passed
    assert metric.fact == "0 с (на 180 с быстрее норматива)"
    assert report.summary["card_fill_ms"] is not None


def test_dds_snapshot_scores_every_queue_card_and_leaves_state_untouched():
    state = dds_state()
    finish_module.score_live_dds_cards(state)
    completed = list(state.desk.completed)
    snapshot = ScoringSnapshot.of(state)

    report = score(snapshot, grammar_says(True, []))

    assert [card.scenario_id for card in report.cards] == ["fire-apartment-l2", "t20-2-stroke"]
    assert [item.title for item in report.result.metrics] == [
        item.title for card in completed for item in card.metrics
    ]
    assert state.desk.completed == completed


def test_handoff_live_and_refreshed_reports_use_operator_scenario_and_same_score():
    state = card_state(handoff=True)
    active = state.desk.ordered()[1]
    state.desk.open(active.card_id)
    active.set_service_status(
        active.dispatched_card.notify[0], ServiceStatus.ACCEPTED,
        "Основание: доклад.\nСведения: принято.",
    )
    # Предусловие ошибки: к завершению активна не карточка курсанта.
    assert state.desk.active.scenario.id == "t01-1-fire-container"
    assert ScoringSnapshot.of(state).scenario.id == "fire-apartment-l2"

    async def live_then_refreshed():
        async with hub.operation(state.session_id):
            await finish_module.finish(state.session_id, state)
        live = dict(state.score["full_report"])
        state.self_assessed = True
        async with hub.operation(state.session_id):
            await finish_module.refresh_archived_report(state.session_id, state)
        return live, state.score["full_report"]

    live, refreshed = asyncio.run(live_then_refreshed())

    assert live["scenario_id"] == refreshed["scenario_id"] == "fire-apartment-l2"
    assert live["score_auto"] == refreshed["score_auto"] == state.score["score_auto"]
    assert live["metrics"] == refreshed["metrics"]
