"""Эталонный диалог: как должен был пройти опрос.

Собирается **кодом** из чек-листа и фактов сценария. Писать его руками нельзя:
методист поправит факт и забудет эталон, и курсант получит штраф за правильный
ответ (docs/spec/SCENARIO-FORMAT.md).

Работает в двух местах: в разборе — «вот какой вопрос стоял на этом шаге»
(docs/product/DEBRIEF.md), и в эталон-плеере на внешнем мониторе, когда
появится предгенерация озвучки (lct-19, требует ключа LLM).
"""

from pydantic import BaseModel

from app.dialog.caller import REVEAL
from app.dialog.persona import BASE_MOOD
from app.domain.events import Mood
from app.scenarios.schema import Scenario


class ReferenceStep(BaseModel):
    """Шаг эталонного опроса: вопрос оператора и ответ звонящего на него."""

    checklist_id: str
    question: str
    fact_id: str | None = None
    answer: str | None = None
    required: bool = False
    hidden: bool = False


class ReferenceDialog(BaseModel):
    scenario_id: str
    first_line: str
    steps: list[ReferenceStep]


def build(scenario: Scenario) -> ReferenceDialog:
    facts = {fact.id: fact for fact in scenario.facts}
    required = set(scenario.ground_truth.required_facts)
    mood = BASE_MOOD.get(scenario.persona.base, Mood.CALM)

    steps: list[ReferenceStep] = []
    for item in scenario.checklist:
        if not item.question:
            continue
        fact = facts.get(item.fact) if item.fact else None
        steps.append(
            ReferenceStep(
                checklist_id=item.id,
                question=item.question,
                fact_id=fact.id if fact else None,
                # Ответ звонящего — тем же шаблоном, каким он отвечает в живом
                # звонке: эталон должен звучать так же, а не литературно.
                answer=REVEAL[mood].format(fact=fact.value) if fact else None,
                required=bool(fact and fact.id in required),
                hidden=bool(fact and fact.hidden),
            )
        )
    return ReferenceDialog(scenario_id=scenario.id, first_line=scenario.first_line, steps=steps)


def missed_steps(scenario: Scenario, revealed: list[str] | None) -> list[ReferenceStep]:
    """Шаги, которые курсант не отработал. `None` — слот-автомата не было."""
    if revealed is None:
        return []
    return [
        step
        for step in build(scenario).steps
        if step.fact_id and step.fact_id not in revealed and step.required
    ]
