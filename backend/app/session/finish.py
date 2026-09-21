"""Завершение занятия: посчитать оценку и положить её в журнал.

Детерминированный слой считается сразу по завершении звонка (lct-12).
Курсанту `score.ready` уходит **только после самооценки**: сначала он сверяет
своё ощущение с объективной картиной, и расхождение — отдельный материал
для преподавателя (docs/product/DEBRIEF.md). Преподаватель и монитор получают
событие сразу: им ждать нечего.
"""

import logging
from uuid import UUID

from app.domain.events import Exercise, Metric, ScoreReady
from app.domain.taxonomy import Competency, ErrorCode, Finding, FindingSource
from app.domain.timers import NORMATIVES, TimerCode
from app.scenarios import store
from app.scoring.competency import radar
from app.scoring.card import evaluate_card
from app.scoring.dispatcher import dispatcher_metrics, evaluate_dispatcher
from app.scoring.gost import GostResult, evaluate
from app.scoring.weights import apply_weights
from app.session.hub import hub
from app.session.state import DdsCardRecord, now_utc

log = logging.getLogger(__name__)


def score_current_dds(state) -> DdsCardRecord:
    """Оценить активную карточку отдельно, до выдачи следующей."""
    number = state.dds_card_index + 1
    findings = evaluate_dispatcher(
        entries=state.status_log,
        services=state.notified_services(),
        deadline_ms=NORMATIVES[TimerCode.DDS_ACK].limit_ms,
        elapsed_ms=state.timers.measured_ms(TimerCode.DDS_ACK),
    )
    metrics = dispatcher_metrics(state, NORMATIVES[TimerCode.DDS_ACK].limit_ms)
    reply = state.reply_text.strip()
    if reply or len(state.dds_scenarios) > 1:
        passed = len(reply) >= 20 and len(reply.split()) >= 3
        metrics.append(Metric(
            key="dds_reply", title="Свободный ответ по карточке",
            fact=reply or "ответ отсутствует",
            norm="содержательный текст не короче 20 символов и 3 слов",
            ref="ТЗ: действия с карточками, ввод текста",
            passed=passed, weight=1.5,
        ))
        if not passed:
            findings.append(Finding(
                code=ErrorCode.D5, source=FindingSource.DISPATCHER,
                summary="Свободный ответ отсутствует или слишком краток",
                fact=reply or "ответ отсутствует",
                norm="не менее 20 символов и 3 слов; грамматика отдельно не проверялась",
                ref="ТЗ: действия с карточками, ввод текста",
                competency=Competency.COMMUNICATION,
            ))
    weighted = GostResult(metrics=metrics, findings=findings)
    apply_weights(weighted, state.scenario.score_weights)
    actions = [
        {"type": "card.status", "service": mark.service, "status": mark.status.value,
         "comment": mark.comment, "at": mark.at.isoformat()}
        for mark in state.status_log
    ] + [
        {"type": "phone.report", "service": report.service, "crew": report.crew,
         "phase": report.phase, "text": report.text, "at": report.at.isoformat()}
        for report in state.phone_reports
    ] + [
        {"type": "card.reply", "text": text, "at": at.isoformat()}
        for at, text in state.reply_log
    ] + [
        {"type": kind, "text": text or "", "at": at.isoformat()}
        for kind, at, text in state.dds_log
    ]
    actions.sort(key=lambda item: item["at"])
    return DdsCardRecord(
        card_id=state.dispatched_card.card_id,
        scenario_id=state.scenario_id,
        reply_text=state.reply_text,
        metrics=[metric.model_copy(update={"title": f"Карточка {number}: {metric.title}"})
                 for metric in weighted.metrics],
        findings=[finding.model_copy(update={"summary": f"Карточка {number}: {finding.summary}"})
                  for finding in weighted.findings],
        actions=actions,
        duration_ms=(max(0, int((now_utc() - state.dispatched_at).total_seconds() * 1000))
                     if state.dispatched_at else 0),
    )


async def finish(session_id: UUID, state) -> None:
    # Сценарий занятия, а не библиотечный: директивы могли поправить эталон.
    scenario = state.scenario or store.get(state.scenario_id)
    if scenario is None:
        return

    if state.exercise is Exercise.CARD:
        result = evaluate_card(scenario, state.dispatched_card or state.kio)
    elif state.exercise is Exercise.DDS:
        # В ДДС могут последовательно пройти несколько карточек. Уже закрытые
        # сохранены отдельными результатами; активную оцениваем лишь однажды.
        result = GostResult()
        cards = list(state.dds_completed)
        if state.dispatched_card and not any(
            item.card_id == state.dispatched_card.card_id for item in cards
        ):
            cards.append(score_current_dds(state))
        state.dds_completed = cards
        for card in cards:
            result.metrics.extend(card.metrics)
            result.findings.extend(card.findings)
        result.unavailable.append(
            "Грамматика свободного ответа: автоматическая проверка не настроена; "
            "оценивались только наличие и минимальная полнота текста"
        )
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
    if state.dispatched_card is not None and (
        state.exercise is Exercise.CALL or state.handoff_to_dds
    ):
        dispatcher_findings = evaluate_dispatcher(
            entries=state.status_log,
            services=state.notified_services(),
            deadline_ms=NORMATIVES[TimerCode.DDS_ACK].limit_ms,
            elapsed_ms=state.timers.measured_ms(TimerCode.DDS_ACK),
        )
        result.findings.extend(dispatcher_findings)
        result.metrics.extend(dispatcher_metrics(state, NORMATIVES[TimerCode.DDS_ACK].limit_ms))
    if state.exercise is not Exercise.DDS:
        apply_weights(result, scenario.score_weights)

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
        "card_results": [
            {"card_id": str(card.card_id), "scenario_id": card.scenario_id,
             "score_auto": card.score_auto, "reply_text": card.reply_text,
             "actions": card.actions, "duration_ms": card.duration_ms,
             "metrics": [metric.model_dump(mode="json") for metric in card.metrics],
             "findings": [finding.model_dump(mode="json") for finding in card.findings]}
            for card in cards
        ] if state.exercise is Exercise.DDS else [],
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
