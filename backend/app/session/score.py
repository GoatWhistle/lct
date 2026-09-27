"""Оценка занятия по неизменяемому снимку.

`finish()` сначала доводит живое занятие до оцениваемого вида (дооценивает
очередь ДДС), затем снимает `ScoringSnapshot` и отдаёт его `score_session`.
Сам расчёт не читает и не меняет `state`: живой, сохранённый и перестроенный
отчёт получают сценарий одним путём — через `scoring_scenario`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.domain.events import CallEndReason, Exercise, LessonCriteria, Metric
from app.domain.kio import KIO
from app.domain.taxonomy import Competency, ErrorCode, Finding, FindingSource
from app.domain.timers import TimerCode
from app.scenarios import store
from app.scenarios.schema import Scenario
from app.scoring.card import evaluate_card
from app.scoring.competency import radar
from app.scoring.dispatcher import dispatcher_metrics, evaluate_dispatcher
from app.scoring.gost import GostResult, evaluate
from app.scoring.grammar import GrammarAssessment, assess
from app.scoring.timing import CARD_FILL_TIME, time_metric
from app.scoring.weights import apply_weights
from app.session.dds import DdsCardRecord, DdsLiveCard
from app.session.timers import SessionTimers

GrammarCheck = Callable[[str], Awaitable[GrammarAssessment]]


def scoring_scenario(state) -> Scenario | None:
    """Сценарий, по которому оценивается и разбирается занятие.

    После передачи 112→ДДС КИО курсанта оценивается по эталону упражнения
    112. Балл, разбор и чек-лист самооценки обязаны брать один и тот же
    сценарий.
    """
    if state.handoff_to_dds and state.operator_scenario:
        return state.operator_scenario
    if state.scenario is None and state.desk.scenarios:
        return state.desk.scenarios[0]
    return state.scenario or store.get(state.scenario_id)


@dataclass(frozen=True)
class ScoringSnapshot:
    """Всё, что нужно для балла, снятое с занятия в момент завершения."""

    scenario: Scenario
    exercise: Exercise
    handoff_to_dds: bool
    #: КИО, которую сверяют с эталоном: в упражнении 112 — сданная карточка.
    kio: KIO
    #: Свободное описание оператора 112 — предмет проверки грамматики.
    description: str
    dispatched: bool
    timers: SessionTimers
    criteria: LessonCriteria
    revealed_facts: tuple[str, ...] | None
    refined_facts: tuple[str, ...] | None
    end_reason: CallEndReason | None
    bounced_fields: tuple[str, ...]
    resolved_outcome: str | None
    hints: int
    #: Уже оценённые карточки очереди ДДС, в порядке выдачи.
    dds_cards: tuple[DdsCardRecord, ...]
    #: Карточка на пульте в упражнении по звонку: отметки D1–D6 рядом с E1–E6.
    call_card: DdsLiveCard | None
    call_card_services: tuple[str, ...]

    @property
    def reports_cards(self) -> bool:
        return self.exercise is Exercise.DDS or self.handoff_to_dds

    @classmethod
    def of(cls, state) -> "ScoringSnapshot | None":
        """Снимок живого занятия; None — оценивать не по чему.

        Очередь ДДС к этому моменту должна быть дооценена
        (`finish.score_live_dds_cards`): снимок берёт готовые записи.
        """
        scenario = scoring_scenario(state)
        if scenario is None:
            return None
        if state.exercise is Exercise.CARD:
            kio = state.operator_kio or state.dispatched_card or state.kio
        else:
            kio = state.kio
        # Грамматика относится к свободному описанию оператора 112, а не к
        # заполнению карточки на стороне ДДС.
        operator_kio = state.operator_kio if state.handoff_to_dds else None
        live_card = state.desk.active if state.exercise is Exercise.CALL else None
        queue = state.exercise is Exercise.DDS or (state.handoff_to_dds and state.desk.scenarios)
        return cls(
            scenario=scenario,
            exercise=state.exercise,
            handoff_to_dds=state.handoff_to_dds,
            kio=kio.model_copy(deep=True),
            description=(operator_kio or state.kio).description or "",
            dispatched=state.dispatched_card is not None,
            timers=state.timers.model_copy(deep=True),
            criteria=state.criteria.model_copy(deep=True),
            revealed_facts=(tuple(fact.id for fact in state.slots.revealed_facts())
                            if state.slots else None),
            refined_facts=tuple(state.slots.refined) if state.slots else None,
            end_reason=state.end_reason,
            bounced_fields=tuple(state.bounced_fields),
            resolved_outcome=state.resolved_outcome,
            hints=len(state.hints_shown),
            dds_cards=tuple(state.desk.completed) if queue else (),
            call_card=live_card.model_copy(deep=True) if live_card is not None else None,
            call_card_services=(tuple(state.card_services(live_card))
                                if live_card is not None else ()),
        )


@dataclass(frozen=True)
class ScoreReport:
    """Посчитанная оценка: результат ГОСТ, сводка числами и карточки ДДС."""

    result: GostResult
    summary: dict[str, Any]
    cards: tuple[DdsCardRecord, ...]
    reports_cards: bool

    def payload(self, *, card_status: str) -> dict[str, Any]:
        """Сохраняемая оценка занятия — без разбора модели и полного отчёта."""
        return {
            "score_auto": self.result.score,
            "summary": self.summary,
            "metrics": [metric.model_dump() for metric in self.result.metrics],
            "findings": [finding.model_dump(mode="json") for finding in self.result.findings],
            "competencies": [item.model_dump() for item in radar(self.result.metrics)],
            "unavailable": self.result.unavailable,
            # Статус карточки — готовая красная метка, понятная любому диспетчеру.
            "card_status": card_status,
            "card_results": [
                {"card_id": str(card.card_id), "scenario_id": card.scenario_id,
                 "score_auto": card.score_auto, "reply_text": card.reply_text,
                 "actions": card.actions, "duration_ms": card.duration_ms,
                 "title": card.title, "address": card.address,
                 "description": card.description, "incident_type": card.incident_type,
                 "victims_count": card.victims_count,
                 "received_at": card.received_at.isoformat() if card.received_at else None,
                 "managed_service": card.managed_service,
                 "recipient_services": card.recipient_services,
                 "metrics": [metric.model_dump(mode="json") for metric in card.metrics],
                 "findings": [finding.model_dump(mode="json") for finding in card.findings]}
                for card in self.cards
            ] if self.reports_cards else [],
        }


async def score_session(
    snapshot: ScoringSnapshot, *, grammar: GrammarCheck = assess
) -> ScoreReport:
    """Балл занятия. Единственный внешний вызов — проверка грамматики, и та
    только по явному критерию преподавателя."""
    scenario = snapshot.scenario
    if snapshot.exercise is Exercise.CARD:
        result = evaluate_card(scenario, snapshot.kio)
        metric, finding = time_metric(
            CARD_FILL_TIME,
            snapshot.timers.measured_ms(TimerCode.CARD_FILL),
            snapshot.timers.limits[TimerCode.CARD_FILL],
            completed=snapshot.dispatched,
        )
        result.metrics.append(metric)
        if finding is not None:
            result.findings.append(finding)
    elif snapshot.exercise is Exercise.DDS:
        # Все карточки выданы одновременно: при досрочном завершении оценена
        # каждая, включая не открытую, потому что её норматив уже шёл.
        result = GostResult()
    else:
        result = evaluate(
            scenario=scenario,
            kio=snapshot.kio,
            timers=snapshot.timers,
            revealed_facts=(list(snapshot.revealed_facts)
                            if snapshot.revealed_facts is not None else None),
            refined_facts=(list(snapshot.refined_facts)
                           if snapshot.refined_facts is not None else None),
            end_reason=snapshot.end_reason,
            bounced_fields=list(snapshot.bounced_fields),
            resolved_outcome=snapshot.resolved_outcome,
            dispatched=snapshot.dispatched,
        )
    # Учитывается только по явной настройке преподавателя и только когда есть
    # текст для проверки.
    if (snapshot.exercise is not Exercise.DDS and snapshot.criteria.require_correct_grammar
            and snapshot.description.strip()):
        verdict = await grammar(snapshot.description)
        result.metrics.append(Metric(
            key="description_grammar",
            title="Грамматика описания происшествия",
            fact="ошибок не обнаружено" if verdict.passed else "; ".join(verdict.errors),
            norm="грамматически корректное описание",
            ref="критерий занятия; правила русского языка",
            passed=verdict.passed,
            weight=1.0,
        ))
        if not verdict.passed:
            result.findings.append(Finding(
                code=ErrorCode.E4,
                source=FindingSource.GRAMMAR,
                summary="Грамматическая ошибка в описании происшествия",
                fact="; ".join(verdict.errors),
                norm="грамматически корректное описание",
                ref="критерий занятия; правила русского языка",
                competency=Competency.COMMUNICATION,
            ))
    # Работа диспетчера — вторая роль и вторая таксономия. Отметки D1–D6 идут
    # рядом с E1–E6, а не вместо: в живой цепочке 112 → ДДС в одном занятии
    # участвуют оба (docs/spec/DATASET.md#статусы-реагирования).
    live_card = snapshot.call_card
    if live_card is not None:
        decision_limit_ms = live_card.timers.limits[TimerCode.DDS_ACK]
        services = list(snapshot.call_card_services)
        result.findings.extend(evaluate_dispatcher(
            entries=live_card.status_log,
            services=services,
            crew_assignments=live_card.crew_assignments,
            deadline_ms=decision_limit_ms,
            elapsed_ms=live_card.timers.measured_ms(TimerCode.DDS_ACK),
            reply_text=live_card.reply_text,
            expected_decision=scenario.dds_decision.expected,
            expected_decision_reason=scenario.dds_decision.reason,
            phone_reports=live_card.phone_reports,
            report_reaction_limit_seconds=snapshot.criteria.dds_report_reaction_limit_seconds,
        ))
        result.metrics.extend(dispatcher_metrics(
            live_card, decision_limit_ms, scenario.dds_decision.expected,
            scenario.dds_decision.reason, services=services,
            report_reaction_limit_seconds=snapshot.criteria.dds_report_reaction_limit_seconds,
        ))
    # Карточки ДДС уже взвешены каждая своим сценарием плюс настройкой
    # занятия. Веса первого сценария поверх них испортили бы остальные.
    if snapshot.exercise is not Exercise.DDS:
        apply_weights(result, {**scenario.score_weights, **snapshot.criteria.score_weights})
    # В связке КИО оценивается относительно эталона и весов упражнения 112,
    # а каждая карточка очереди уже взвешена собственным сценарием.
    for card in snapshot.dds_cards:
        result.metrics.extend(card.metrics)
        result.findings.extend(card.findings)

    # Сводка числами: по ней считается дельта между попытками в профиле.
    # Вытаскивать её разбором текста метрик («94 с») — путь к тихим ошибкам.
    required = scenario.ground_truth.required_facts
    revealed = snapshot.revealed_facts or ()
    codes: dict[str, int] = {}
    for finding in result.findings:
        codes[finding.code.value] = codes.get(finding.code.value, 0) + 1
    summary = {
        "interview_ms": snapshot.timers.measured_ms(TimerCode.INTERVIEW),
        "card_fill_ms": snapshot.timers.measured_ms(TimerCode.CARD_FILL),
        "facts_got": len([fact for fact in required if fact in revealed]),
        "facts_required": len(required),
        "hints": snapshot.hints,
        "codes": codes,
    }
    return ScoreReport(
        result=result, summary=summary, cards=snapshot.dds_cards,
        reports_cards=snapshot.reports_cards,
    )
