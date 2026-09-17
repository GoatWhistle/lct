"""Эталонный диалог собирается из фактов, а не пишется руками."""

from pathlib import Path

import pytest

from app.scenarios.loader import load_file
from app.scoring.reference import build, missed_steps

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


@pytest.fixture(scope="module")
def scenario():
    return load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)


def test_every_checklist_question_becomes_a_step(scenario):
    reference = build(scenario)
    assert [step.checklist_id for step in reference.steps] == [
        item.id for item in scenario.checklist if item.question
    ]
    assert reference.first_line == scenario.first_line


def test_answers_come_from_facts_not_from_prose(scenario):
    """Эталон не может разойтись с фактами: ответы взяты из них же."""
    reference = build(scenario)
    address = next(step for step in reference.steps if step.fact_id == "f_address")
    fact = next(fact for fact in scenario.facts if fact.id == "f_address")
    assert fact.value in address.answer


def test_step_without_a_fact_has_no_answer(scenario):
    """«Представьтесь» факта не добывает — и ответа в эталоне у него нет."""
    caller = next(step for step in build(scenario).steps if step.checklist_id == "q_caller")
    assert caller.fact_id is None and caller.answer is None
    assert not caller.required


def test_missed_steps_are_only_the_required_ones(scenario):
    missed = missed_steps(scenario, revealed=["f_address"])
    assert [step.fact_id for step in missed] == ["f_what_burns", "f_people", "f_smoke", "f_gas"]
    assert all(step.question for step in missed), "в разборе нужен текст эталонного вопроса"


def test_without_slot_machine_nothing_is_declared_missed(scenario):
    assert missed_steps(scenario, revealed=None) == []
