"""Завершение занятия: посчитать оценку и положить её в журнал.

Детерминированный слой считается сразу по завершении звонка (lct-12).
Курсанту `score.ready` уходит **только после самооценки**: сначала он сверяет
своё ощущение с объективной картиной, и расхождение — отдельный материал
для преподавателя (docs/product/DEBRIEF.md). Преподаватель и монитор получают
событие сразу: им ждать нечего.
"""

import logging
from uuid import UUID

from app.domain.events import Exercise, ScoreReady
from app.domain.timers import NORMATIVES, TimerCode
from app.scenarios import store
from app.scoring.competency import radar
from app.scoring.card import evaluate_card
from app.scoring.dispatcher import dispatcher_metrics, evaluate_dispatcher
from app.scoring.gost import GostResult, evaluate
from app.session.hub import hub

log = logging.getLogger(__name__)


async def finish(session_id: UUID, state) -> None:
    # Сценарий занятия, а не библиотечный: директивы могли поправить эталон.
    scenario = state.scenario or store.get(state.scenario_id)
    if scenario is None:
        return

    if state.exercise is Exercise.CARD:
        result = evaluate_card(scenario, state.dispatched_card or state.kio)
    elif state.exercise is Exercise.DDS:
        # В готовой карточке не было входящего звонка, опроса и действий
        # оператора 112. Ни одна E-метрика здесь не применима.
        result = GostResult()
    else:
        result = evaluate(
            scenario=scenario,
            kio=state.kio,
            timers=state.timers,
            revealed_facts=[fact.id for fact in state.slots.revealed_facts()] if state.slots else None,
            refined_facts=list(state.slots.refined) if state.slots else None,
            end_reason=state.end_reason,
            bounced_fields=state.bounced_fields,
            resolved_outcome=state.resolved_outcome,
            dispatched=state.dispatched_card is not None,
        )
    # Работа диспетчера — вторая роль и вторая таксономия. Отметки D1–D6 идут
    # рядом с E1–E6, а не вместо: в живой цепочке 112 → ДДС в одном занятии
    # участвуют оба (docs/spec/DATASET.md#статусы-реагирования).
    if state.dispatched_card is not None and state.exercise is not Exercise.CARD:
        dispatcher_findings = evaluate_dispatcher(
            entries=state.status_log,
            services=state.notified_services(),
            deadline_ms=NORMATIVES[TimerCode.DDS_ACK].limit_ms,
            elapsed_ms=state.timers.measured_ms(TimerCode.DDS_ACK),
        )
        result.findings.extend(dispatcher_findings)
        result.metrics.extend(dispatcher_metrics(state, NORMATIVES[TimerCode.DDS_ACK].limit_ms))

    # Сводка числами: по ней считается дельта между попытками в профиле.
    # Вытаскивать её разбором текста метрик («94 с») — путь к тихим ошибкам.
    required = scenario.ground_truth.required_facts
    revealed = [fact.id for fact in state.slots.revealed_facts()] if state.slots else []
    codes: dict[str, int] = {}
    for finding in result.findings:
        codes[finding.code.value] = codes.get(finding.code.value, 0) + 1

    state.score = {
        "score_auto": result.score,
        "summary": {
            "interview_ms": state.timers.measured_ms(TimerCode.INTERVIEW),
            "facts_got": len([fact for fact in required if fact in revealed]),
            "facts_required": len(required),
            "hints": len(state.hints_shown),
            "codes": codes,
        },
        "metrics": [metric.model_dump() for metric in result.metrics],
        "findings": [finding.model_dump(mode="json") for finding in result.findings],
        "competencies": [item.model_dump() for item in radar(result.metrics)],
        "unavailable": result.unavailable,
        # Статус карточки — готовая красная метка, понятная любому диспетчеру.
        "card_status": state.station_snapshot().card.value,
    }
    log.info("сессия %s: оценка %.1f, отметок %d", session_id, result.score, len(result.findings))

    if hub.journal:
        await hub.journal.score(session_id, result.score, state.score)

    hub.to_observers(session_id, ScoreReady(session_id=session_id))
    await release_score(session_id, state)


async def release_score(session_id: UUID, state) -> None:
    """Отдать оценку курсанту, когда самооценка сдана."""
    if state.score is not None and (state.self_assessed or state.exercise is Exercise.CARD):
        hub.to_trainee(session_id, ScoreReady(session_id=session_id))
