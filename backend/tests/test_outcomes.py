"""Исход вызова: не каждый звонок заканчивается карточкой.

Пункты приёмки lct-36. Материал — билет 1 заказчика: третий вызов в нём
из Волгоградской области, и правильное действие — передать, а не заводить
карточку (docs/spec/TICKETS.md).
"""

from pathlib import Path

import pytest

from app.domain.classifiers import Outcome
from app.domain.kio import KIO
from app.scenarios.loader import load_file, load_library
from app.scoring.gost import evaluate
from app.session.timers import SessionTimers

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


@pytest.fixture(scope="module")
def other_region():
    return load_file(LIBRARY / "tickets" / "t01-3-child-other-region.yaml", LIBRARY)


@pytest.fixture(scope="module")
def ordinary():
    return load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)


def timeline() -> SessionTimers:
    timers = SessionTimers()
    timers.on_event("call.incoming", now=0.0)
    timers.on_event("call.answer", now=5.0)
    return timers


def score(scenario, *, resolved=None, dispatched=False, kio=None):
    return evaluate(
        scenario=scenario,
        kio=kio or KIO(),
        timers=timeline(),
        revealed_facts=[fact.id for fact in scenario.facts],
        refined_facts=[fact.id for fact in scenario.facts if fact.refine_on],
        resolved_outcome=resolved,
        dispatched=dispatched,
    )


def test_default_outcome_is_a_card(ordinary):
    assert ordinary.outcome is Outcome.CARD


def test_transfer_is_declared_by_the_scenario(other_region):
    assert other_region.outcome is Outcome.TRANSFER_REGION


def test_correct_transfer_passes(other_region):
    result = score(other_region, resolved="transfer_region")
    metric = next(m for m in result.metrics if m.key == "outcome")
    assert metric.passed


def test_filing_a_card_instead_of_transferring_is_an_error(other_region):
    """Курсант завёл карточку и отправил наряд в другой субъект — это ошибка,
    которую прежняя оценка не видела вовсе."""
    result = score(other_region, dispatched=True)
    metric = next(m for m in result.metrics if m.key == "outcome")
    assert not metric.passed
    assert any(finding.code.value == "E2" for finding in result.findings)


def test_doing_nothing_is_also_an_error(other_region):
    result = score(other_region)
    assert not next(m for m in result.metrics if m.key == "outcome").passed


def test_empty_card_is_not_punished_when_no_card_is_expected(other_region):
    """Штрафовать за пустые поля там, где карточка не заводится, значит учить
    заполнять лишнее."""
    result = score(other_region, resolved="transfer_region")
    keys = {metric.key for metric in result.metrics}
    assert "required_fields" not in keys
    assert "address" not in keys
    assert "victims_count" not in keys


def test_ordinary_call_has_no_outcome_metric(ordinary):
    """У обычного вызова исход не обсуждается — метрики просто нет,
    а не «пройдена»."""
    result = score(ordinary, dispatched=True)
    assert "outcome" not in {metric.key for metric in result.metrics}


def test_interview_still_counts_for_a_transfer(other_region):
    """Передать вызов — не значит не опрашивать: адрес и состояние ребёнка
    другому региону всё равно нужны."""
    result = score(other_region, resolved="transfer_region")
    assert "checklist_completeness" in {metric.key for metric in result.metrics}


# ── билет как единица занятия ──


def test_ticket_one_is_three_calls_of_different_services():
    """Билет собран так, чтобы за одно занятие курсант прошёл разные службы."""
    ticket = [s for s in load_library(LIBRARY) if s.ticket == 1]
    assert len(ticket) == 3
    assert sorted(s.position for s in ticket) == [1, 2, 3]
    assert len({s.type for s in ticket}) == 3


def test_ticket_and_position_go_together():
    from pydantic import ValidationError

    from app.scenarios.schema import Scenario

    with pytest.raises(ValidationError):
        Scenario.model_validate(
            {
                "id": "broken",
                "title": "Билет без номера вызова",
                "type": "fire",
                "level": "L1",
                "ticket": 4,
                "persona": {"base": "calm"},
                "first_line": "Алло",
            }
        )


def test_mass_fight_notifies_both_police_and_medics():
    """Тот случай, где выбор одной службы был бы неверен по построению."""
    scenario = load_file(LIBRARY / "tickets" / "t01-2-mass-fight.yaml", LIBRARY)
    assert "МВД" in scenario.ground_truth.notify
    assert "СМП" in scenario.ground_truth.notify
