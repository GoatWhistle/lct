"""Сборка отчёта сессии — единицы истории.

Из отчётов складываются профиль курсанта, дельта попыток и аналитика группы.
Ни одной отметки без обоснования: у каждой есть код, факт и норматив
(docs/product/DEBRIEF.md).
"""

from uuid import UUID

from app.domain.events import (
    CompetencyScore,
    HintShown,
    HintUsage,
    InstructorNoteShown,
    Metric,
    SelfAssessment,
    SelfAssessmentDiff,
    SessionReport,
)
from app.domain.taxonomy import Finding
from app.scenarios.schema import Scenario
from app.scoring.reference import build as build_reference


def assess_difference(
    scenario: Scenario, revealed: list[str] | None, missed_claimed: list[str]
) -> SelfAssessmentDiff:
    """Сверить, что курсант считает пропущенным, с тем, что он пропустил на деле."""
    reference = build_reference(scenario)
    required = {step.checklist_id: step.fact_id for step in reference.steps if step.required}
    really_missed = {
        checklist_id
        for checklist_id, fact_id in required.items()
        if revealed is not None and fact_id not in revealed
    }
    claimed = set(missed_claimed)
    return SelfAssessmentDiff(
        noticed=sorted(claimed & really_missed),
        unnoticed=sorted(really_missed - claimed),
        overcautious=sorted(claimed - really_missed),
    )


def build(session_id: UUID, state, scenario: Scenario) -> SessionReport:
    score = state.score or {}
    revealed = [fact.id for fact in state.slots.revealed_facts()] if state.slots else None
    reference = build_reference(scenario)

    self_assessment = None
    difference = None
    if state.self_assessment is not None:
        self_assessment = SelfAssessment(
            missed=state.self_assessment["missed"],
            comment=state.self_assessment.get("comment", ""),
            submitted_at=state.ended_at or state.transcript[-1].at,
        )
        difference = assess_difference(scenario, revealed, self_assessment.missed)

    questions = {step.checklist_id: step.question for step in reference.steps}
    missed_checklist = [
        step.checklist_id
        for step in reference.steps
        if step.required and revealed is not None and step.fact_id not in revealed
    ]

    return SessionReport(
        session_id=session_id,
        scenario_id=scenario.id,
        mode=state.mode,
        attempt=state.attempt,
        transcript=list(state.transcript),
        findings=[Finding.model_validate(item) for item in score.get("findings", [])],
        metrics=[Metric.model_validate(item) for item in score.get("metrics", [])],
        competencies=[CompetencyScore.model_validate(item) for item in score.get("competencies", [])],
        # Эталонные вопросы по шагам: по каждому пропущенному пункту видно,
        # какой вопрос был правильным.
        reference_questions=[
            HintShown(checklist_id=step.checklist_id, question=step.question)
            for step in reference.steps
        ],
        missed_checklist=missed_checklist,
        hints_used=[
            HintUsage(checklist_id=checklist_id, question=questions.get(checklist_id, ""), at=at)
            for checklist_id, at in state.hints_log
        ],
        self_assessment=self_assessment,
        self_assessment_diff=difference,
        notes=[InstructorNoteShown.model_validate(note) for note in state.notes],
        score_auto=score.get("score_auto", 0.0),
        score_final=score.get("score_final", score.get("score_auto", 0.0)),
        overridden_by=score.get("overridden_by"),
        override_comment=score.get("override_comment"),
    )
