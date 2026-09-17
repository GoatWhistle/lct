"""Детерминированная оценка по ГОСТ.

Проверяются пункты приёмки lct-12: воспроизводимость, ни одной отметки без
обоснования, время опроса, полнота опроса, неверная ДДС.
"""

import json
from pathlib import Path

import pytest

from app.domain.events import CallEndReason
from app.domain.kio import KIO
from app.domain.taxonomy import Competency, ErrorCode
from app.scenarios.loader import load_file
from app.scoring.competency import radar
from app.scoring.gost import evaluate
from app.session.timers import SessionTimers

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"
ALL_FACTS = ["f_address", "f_what_burns", "f_people", "f_smoke", "f_gas"]


@pytest.fixture(scope="module")
def scenario():
    return load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)


def timeline(answer_s: float = 5, interview_s: float = 60) -> SessionTimers:
    """Таймеры с явными моментами событий — без часов, чтобы результат не плыл."""
    timers = SessionTimers()
    timers.on_event("call.incoming", now=0.0)
    timers.on_event("call.answer", now=answer_s)
    timers.on_event("dds.dispatch", now=answer_s + interview_s)
    return timers


def good_card() -> KIO:
    return KIO(
        address="ул. Ленина, д. 14, кв. 47",
        floor="5",
        incident_type="fire",
        dds="01",
        victims_count=2,
        description="горит балкон",
    )


def test_perfect_call_passes_everything(scenario):
    result = evaluate(scenario=scenario, kio=good_card(), timers=timeline(), revealed_facts=ALL_FACTS)
    failed = [metric.key for metric in result.metrics if not metric.passed]
    assert failed == [], f"провалено: {failed}"
    assert result.findings == []
    assert result.score == 100.0


def test_same_call_gives_identical_json(scenario):
    """Оценку можно предъявить и проверить только если она повторяется."""
    def run():
        result = evaluate(
            scenario=scenario, kio=KIO(dds="03"), timers=timeline(interview_s=94),
            revealed_facts=["f_address"],
        )
        return json.dumps(
            {
                "metrics": [m.model_dump() for m in result.metrics],
                "findings": [f.model_dump(mode="json") for f in result.findings],
                "score": result.score,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    assert run() == run()


def test_interview_over_75_seconds_is_e3_with_the_numbers(scenario):
    result = evaluate(scenario=scenario, kio=good_card(), timers=timeline(interview_s=94), revealed_facts=ALL_FACTS)
    metric = next(m for m in result.metrics if m.key == "interview_time")
    assert not metric.passed
    assert (metric.fact, metric.norm) == ("94 с", "≤ 75 с")
    assert metric.ref == "ГОСТ Р 22.7.03-2021"

    finding = next(f for f in result.findings if f.code is ErrorCode.E3)
    assert "94 с" in finding.summary and "75 с" in finding.summary


def test_interview_never_stopped_is_a_failure_not_a_pass(scenario):
    """Таймер не остановлен событием — время недоказуемо. Молча засчитать
    такую метрику значит поставить зачёт за непереданную карточку."""
    timers = SessionTimers()
    timers.on_event("call.incoming", now=0.0)
    timers.on_event("call.answer", now=3.0)
    result = evaluate(scenario=scenario, kio=good_card(), timers=timers, revealed_facts=ALL_FACTS)
    metric = next(m for m in result.metrics if m.key == "interview_time")
    assert not metric.passed and "не зафиксировано" in metric.fact


def test_each_missing_fact_is_its_own_e1_with_the_reference_question(scenario):
    """В разборе нужен конкретный пропущенный вопрос, а не процент."""
    result = evaluate(
        scenario=scenario, kio=good_card(), timers=timeline(),
        revealed_facts=["f_address", "f_what_burns", "f_people"],
    )
    e1 = [f for f in result.findings if f.code is ErrorCode.E1]
    assert len(e1) == 2
    assert all("эталонный вопрос" in f.norm for f in e1)

    completeness = next(m for m in result.metrics if m.key == "checklist_completeness")
    assert completeness.fact == "добыто 3 из 5 обязательных фактов"


def test_wrong_dds_is_e2(scenario):
    card = good_card()
    card.dds = "03"
    result = evaluate(scenario=scenario, kio=card, timers=timeline(), revealed_facts=ALL_FACTS)
    finding = next(f for f in result.findings if f.code is ErrorCode.E2)
    assert "03" in finding.summary and "01" in finding.summary


def test_address_abbreviations_are_not_punished(scenario):
    for written in ("улица Ленина, 14", "ул. Ленина д. 14 кв. 47", "Ленина 14"):
        card = good_card()
        card.address = written
        result = evaluate(scenario=scenario, kio=card, timers=timeline(), revealed_facts=ALL_FACTS)
        assert next(m for m in result.metrics if m.key == "address").passed, written


def test_wrong_building_is_caught(scenario):
    card = good_card()
    card.address = "улица Ленина, 41"
    result = evaluate(scenario=scenario, kio=card, timers=timeline(), revealed_facts=ALL_FACTS)
    assert not next(m for m in result.metrics if m.key == "address").passed


def test_no_finding_without_justification(scenario):
    """Ни одной отметки без кода и обоснования — ни в одном интерфейсе."""
    result = evaluate(
        scenario=scenario, kio=KIO(), timers=SessionTimers(), revealed_facts=[],
        end_reason=CallEndReason.DROPPED,
    )
    assert result.findings, "пустая карточка должна дать отметки"
    for finding in result.findings:
        assert finding.code and finding.summary and finding.fact and finding.norm, finding


def test_without_slot_machine_completeness_is_not_silently_passed(scenario):
    result = evaluate(scenario=scenario, kio=good_card(), timers=timeline(), revealed_facts=None)
    assert "checklist_completeness" not in [m.key for m in result.metrics]
    assert any("checklist_completeness" in note for note in result.unavailable)


def test_dropped_call_without_callback_fails(scenario):
    result = evaluate(
        scenario=scenario, kio=good_card(), timers=timeline(), revealed_facts=ALL_FACTS,
        end_reason=CallEndReason.DROPPED,
    )
    callback = next(m for m in result.metrics if m.key == "callback")
    assert not callback.passed and callback.fact == "обратного дозвона не было"


def test_radar_is_a_projection_of_the_same_metrics(scenario):
    card = good_card()
    card.dds = "03"
    result = evaluate(scenario=scenario, kio=card, timers=timeline(interview_s=94), revealed_facts=ALL_FACTS)
    values = {score.competency: score.value for score in radar(result.metrics)}

    assert values["routing"] == 0.5, "из двух метрик маршрутизации провалена одна"
    assert values["card"] == 1.0
    assert Competency.COMMUNICATION.value not in values, "без судьи коммуникация не оценивалась, а не провалена"


def test_legal_interview_is_not_an_e3(scenario):
    """70 с опроса укладываются в норматив 75 с. Раньше таймер оповещения ДДС
    стартовал на ответе, мерил тот же отрезок с лимитом 60 с и выставлял E3
    за законный опрос. Пока нет события конца опроса, он не считается."""
    result = evaluate(scenario=scenario, kio=good_card(), timers=timeline(interview_s=70), revealed_facts=ALL_FACTS)
    assert [f for f in result.findings if f.code is ErrorCode.E3] == []
    assert any(note.startswith("dds_notify_time") for note in result.unavailable)
