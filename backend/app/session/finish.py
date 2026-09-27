"""Завершение занятия: посчитать оценку и положить её в журнал.

Детерминированный слой считается сразу по завершении звонка (lct-12).
Курсанту `score.ready` уходит **только после самооценки**: сначала он сверяет
своё ощущение с объективной картиной, и расхождение — отдельный материал
для преподавателя (docs/product/DEBRIEF.md). Преподаватель и монитор получают
событие сразу: им ждать нечего.
"""

import asyncio
import logging
import time
from uuid import UUID

from app.domain.events import CallEnded, CallEndReason, Exercise, Metric, ScoreReady, SessionEnded
from app.domain.statuses import ServiceStatus, current
from app.domain.taxonomy import Competency, ErrorCode, Finding, FindingSource
from app.domain.timers import TimerCode
from app.scenarios import store
from app.scoring.ai_coach import coach
from app.scoring.card import evaluate_card
from app.scoring.competency import radar
from app.scoring.dispatcher import dispatcher_metrics, evaluate_dispatcher
from app.scoring.gost import GostResult, evaluate
from app.scoring.grammar import assess
from app.scoring.report import build as build_report
from app.scoring.taxonomy import METRIC_WEIGHTS
from app.scoring.timing import time_credit
from app.scoring.weights import apply_weights
from app.session.hub import hub
from app.session.state import DdsCardRecord, DdsLiveCard, now_utc
from app.session.store import (
    LessonEnded,
    ScoreArchived,
    ScoreCalculated,
    ScoreOverridden,
    apply_score_override,
)

log = logging.getLogger(__name__)


def score_dds_card(state, card: DdsLiveCard) -> DdsCardRecord:
    """Оценить одну карточку пульта; активная карточка при этом не меняется.

    Из сессии берутся только служба обучающегося и критерии занятия.
    """
    number = card.original_index + 1
    services = state.card_services(card)
    decision_limit_ms = card.timers.limits[TimerCode.DDS_ACK]
    dds_decision = card.scenario.dds_decision
    findings = evaluate_dispatcher(
        entries=card.status_log,
        services=services,
        crew_assignments=card.crew_assignments,
        deadline_ms=decision_limit_ms,
        elapsed_ms=card.timers.measured_ms(TimerCode.DDS_ACK),
        reply_text=card.reply_text,
        expected_decision=dds_decision.expected,
        expected_decision_reason=dds_decision.reason,
    )
    metrics = dispatcher_metrics(
        card, decision_limit_ms, dds_decision.expected, dds_decision.reason,
        services=services,
    )
    weighted = GostResult(metrics=metrics, findings=findings)
    work_limit_ms = card.timers.limits[TimerCode.DDS_WORK]
    work_timer = card.timers.timers.get(TimerCode.DDS_WORK)
    work_elapsed_ms = card.timers.measured_ms(TimerCode.DDS_WORK)
    if work_elapsed_ms is None and work_timer is not None and work_timer.started_at is not None:
        work_elapsed_ms = work_timer.current_ms(time.monotonic())
    terminal = bool(services) and all(
        current(card.status_log, service) in {
            ServiceStatus.COMPLETED,
            ServiceStatus.DECLINED,
            ServiceStatus.REFUSED,
        }
        for service in services
    )
    work_passed = terminal and work_elapsed_ms is not None and work_elapsed_ms <= work_limit_ms
    work_delta_ms = (work_elapsed_ms - work_limit_ms) if work_elapsed_ms is not None else None
    if work_elapsed_ms is None:
        work_fact = "время обработки не зафиксировано"
    elif work_delta_ms and work_delta_ms > 0:
        work_fact = f"{round(work_elapsed_ms / 1000)} с (+{round(work_delta_ms / 1000)} с сверх норматива)"
    elif work_delta_ms and work_delta_ms < 0:
        work_fact = (f"{round(work_elapsed_ms / 1000)} с (на "
                     f"{round(abs(work_delta_ms) / 1000)} с быстрее норматива)")
    else:
        work_fact = f"{round(work_elapsed_ms / 1000)} с (точно в норматив)"
    if not terminal:
        work_fact = f"карточка не завершена; {work_fact}"
    weighted.metrics.append(Metric(
        key="dds_work_time",
        title="Отработка карточки ДДС",
        fact=work_fact,
        norm=f"завершить за {round(work_limit_ms / 1000)} с",
        ref="ТЗ задачи № 9; сессия вопросов и ответов 16.09.2026",
        passed=work_passed,
        weight=METRIC_WEIGHTS["dds_work_time"],
        credit=time_credit(work_elapsed_ms, work_limit_ms) if terminal else 0.0,
    ))
    if not work_passed:
        weighted.findings.append(Finding(
            code=ErrorCode.E3,
            source=FindingSource.TIMERS,
            summary="ДДС: норматив времени отработки карточки не выполнен",
            fact=work_fact,
            norm=f"завершить за {round(work_limit_ms / 1000)} с",
            ref="ТЗ задачи № 9; сессия вопросов и ответов 16.09.2026",
            competency=Competency.NORMS,
        ))
    apply_weights(weighted, {**card.scenario.score_weights, **state.criteria.score_weights})
    actions = [
        {"type": "card.status", "service": mark.service, "status": mark.status.value,
         "comment": mark.comment, "at": mark.at.isoformat()}
        for mark in card.status_log
    ] + [
        {"type": "phone.report", "service": report.service, "crew": report.crew,
         "phase": report.phase, "text": report.text, "at": report.at.isoformat()}
        for report in card.phone_reports
    ] + [
        {"type": "card.reply", "text": text, "at": at.isoformat()}
        for at, text in card.reply_log
    ] + [
        {"type": kind, "text": text or "", "at": at.isoformat()}
        for kind, at, text in card.dds_log
    ]
    actions.sort(key=lambda item: item["at"])
    return DdsCardRecord(
        card_id=card.card_id,
        scenario_id=card.scenario.id,
        reply_text=card.reply_text,
        metrics=[metric.model_copy(update={"title": f"Карточка {number}: {metric.title}"})
                 for metric in weighted.metrics],
        findings=[finding.model_copy(update={"summary": f"Карточка {number}: {finding.summary}"})
                  for finding in weighted.findings],
        actions=actions,
        duration_ms=max(0, int((now_utc() - card.dispatched_at).total_seconds() * 1000)),
        title=card.scenario.title,
        address=card.dispatched_card.address,
        description=card.dispatched_card.description,
        incident_type=(card.dispatched_card.incident_type.value
                       if card.dispatched_card.incident_type else None),
        victims_count=card.dispatched_card.victims_count,
        received_at=card.dispatched_at,
        managed_service=services[0] if services else None,
        recipient_services=list(card.dispatched_card.notify),
    )


def scoring_scenario(state):
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


def score_live_dds_cards(state) -> list[DdsCardRecord]:
    """Все карточки занятия: завершённые и оценка ещё открытых.

    Открытые карточки оцениваются по месту, пульт не переключается: отчёт
    берёт card_status у карточки, активной у курсанта при завершении.
    """
    desk = state.desk
    cards = list(desk.completed)
    for live in desk.ordered():
        if not desk.is_completed(live.card_id):
            cards.append(score_dds_card(state, live))
    desk.completed = cards
    return cards


async def finish(session_id: UUID, state) -> None:
    if state.recorder is not None:
        path = await asyncio.to_thread(state.recorder.finalize)
        state.recording_path = str(path) if path else None
    # Сценарий занятия, а не библиотечный: директивы могли поправить эталон.
    scenario = scoring_scenario(state)
    if scenario is None:
        return

    cards: list[DdsCardRecord] = []
    if state.exercise is Exercise.CARD:
        result = evaluate_card(
            scenario, state.operator_kio or state.dispatched_card or state.kio
        )
        limit_ms = state.timers.limits[TimerCode.CARD_FILL]
        elapsed_ms = state.timers.measured_ms(TimerCode.CARD_FILL)
        submitted = state.dispatched_card is not None
        if elapsed_ms is None:
            elapsed_fact = "время не зафиксировано"
        else:
            delta_ms = elapsed_ms - limit_ms
            elapsed_seconds = round(elapsed_ms / 1000)
            if delta_ms > 0:
                deviation = f"+{round(delta_ms / 1000)} с сверх норматива"
            elif delta_ms < 0:
                deviation = f"на {round(abs(delta_ms) / 1000)} с быстрее норматива"
            else:
                deviation = "точно в норматив"
            elapsed_fact = f"{elapsed_seconds} с ({deviation})"
        passed = submitted and elapsed_ms is not None and elapsed_ms <= limit_ms
        result.metrics.append(Metric(
            key="card_fill_time",
            title="Время заполнения карточки",
            fact=elapsed_fact if submitted else f"карточка не сдана; {elapsed_fact}",
            norm=f"сдать карточку за {round(limit_ms / 1000)} с",
            ref="ТЗ задачи № 9; сессия вопросов и ответов 16.09.2026",
            passed=passed,
            weight=METRIC_WEIGHTS["card_fill_time"],
            credit=time_credit(elapsed_ms, limit_ms) if submitted else 0.0,
        ))
        if not passed:
            result.findings.append(Finding(
                code=ErrorCode.E3,
                source=FindingSource.TIMERS,
                summary="Время заполнения карточки: норматив не выполнен",
                fact=elapsed_fact if submitted else f"карточка не сдана; {elapsed_fact}",
                norm=f"сдать карточку за {round(limit_ms / 1000)} с",
                ref="ТЗ задачи № 9; сессия вопросов и ответов 16.09.2026",
                competency=Competency.NORMS,
            ))
    elif state.exercise is Exercise.DDS:
        # Все карточки выданы одновременно: при досрочном завершении оцениваем
        # каждую, включая не открытую, потому что её норматив уже шёл.
        result = GostResult()
        cards = score_live_dds_cards(state)
        for card in cards:
            result.metrics.extend(card.metrics)
            result.findings.extend(card.findings)
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
    # Грамматика относится к свободному описанию оператора 112, а не к
    # заполнению карточки на стороне ДДС. Учитывается только по явной настройке
    # преподавателя и только когда есть текст для проверки.
    operator_kio = state.operator_kio if state.handoff_to_dds else None
    description = (operator_kio or state.kio).description or ""
    if (state.exercise is not Exercise.DDS and state.criteria.require_correct_grammar
            and description.strip()):
        grammar = await assess(description)
        result.metrics.append(Metric(
            key="description_grammar",
            title="Грамматика описания происшествия",
            fact="ошибок не обнаружено" if grammar.passed else "; ".join(grammar.errors),
            norm="грамматически корректное описание",
            ref="критерий занятия; правила русского языка",
            passed=grammar.passed,
            weight=1.0,
        ))
        if not grammar.passed:
            result.findings.append(Finding(
                code=ErrorCode.E4,
                source=FindingSource.GRAMMAR,
                summary="Грамматическая ошибка в описании происшествия",
                fact="; ".join(grammar.errors),
                norm="грамматически корректное описание",
                ref="критерий занятия; правила русского языка",
                competency=Competency.COMMUNICATION,
            ))
    # Работа диспетчера — вторая роль и вторая таксономия. Отметки D1–D6 идут
    # рядом с E1–E6, а не вместо: в живой цепочке 112 → ДДС в одном занятии
    # участвуют оба (docs/spec/DATASET.md#статусы-реагирования).
    live_card = state.desk.active
    if live_card is not None and state.exercise is Exercise.CALL:
        decision_limit_ms = live_card.timers.limits[TimerCode.DDS_ACK]
        services = state.card_services(live_card)
        dispatcher_findings = evaluate_dispatcher(
            entries=live_card.status_log,
            services=services,
            crew_assignments=live_card.crew_assignments,
            deadline_ms=decision_limit_ms,
            elapsed_ms=live_card.timers.measured_ms(TimerCode.DDS_ACK),
            reply_text=live_card.reply_text,
            expected_decision=scenario.dds_decision.expected,
            expected_decision_reason=scenario.dds_decision.reason,
        )
        result.findings.extend(dispatcher_findings)
        result.metrics.extend(dispatcher_metrics(
            live_card, decision_limit_ms, scenario.dds_decision.expected,
            scenario.dds_decision.reason, services=services,
        ))
    # DDS cards were weighted individually in score_dds_card using each
    # card's scenario defaults plus the lesson override. Reapplying the first
    # scenario's weights here would corrupt the other ticket cards.
    if state.exercise is not Exercise.DDS:
        apply_weights(result, {**scenario.score_weights, **state.criteria.score_weights})

    if state.handoff_to_dds and state.desk.scenarios:
        # В связке КИО оценивается относительно эталона и весов упражнения
        # 112, а каждая карточка очереди уже взвешена собственным сценарием.
        cards = score_live_dds_cards(state)
        for card in cards:
            result.metrics.extend(card.metrics)
            result.findings.extend(card.findings)

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
            "card_fill_ms": state.timers.measured_ms(TimerCode.CARD_FILL),
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
             "title": card.title, "address": card.address,
             "description": card.description, "incident_type": card.incident_type,
             "victims_count": card.victims_count,
             "received_at": card.received_at.isoformat() if card.received_at else None,
             "managed_service": card.managed_service,
             "recipient_services": card.recipient_services,
             "metrics": [metric.model_dump(mode="json") for metric in card.metrics],
             "findings": [finding.model_dump(mode="json") for finding in card.findings]}
            for card in cards
        ] if state.exercise is Exercise.DDS or state.handoff_to_dds else [],
    }
    # Модель только поясняет уже посчитанные провалы и балл не трогает; без
    # запущенной модели разбор выходит со статусом «недоступно», а не ждёт её.
    state.score["ai_coaching"] = (await coach(result.metrics)).model_dump(mode="json")
    # Полный разбор хранится вместе с оценкой: PDF/CSV и история должны
    # переживать перезапуск backend, а не зависеть от живого объекта в реестре хаба.
    state.score["full_report"] = build_report(session_id, state, scenario).model_dump(mode="json")
    log.info("сессия %s: оценка %.1f, отметок %d", session_id, result.score, len(result.findings))

    # Оценка и аудит уходят в commit операции, завершившей занятие; ScoreReady
    # рассылает `end_session` и ждёт того же commit — без записи итог не выдаётся.
    hub.record(session_id, ScoreCalculated(result.score, state.score))


def _ends_on_station(state) -> bool:
    """Итог занятия видит пульт ДДС: упражнение ДДС или связка, где карточка
    уже передана диспетчеру. Иначе конец звонка узнаёт курсант 112."""
    return state.exercise is Exercise.DDS or (
        state.handoff_to_dds and state.dispatched_card is not None
    )


async def end_session(session_id: UUID, state, reason: CallEndReason) -> None:
    """Единственный переход занятия в «завершено» — для станции, пульта и звонка.

    Вызывается внутри уже открытой `hub.operation`: строки журнала и события
    уходят её commit, сверка `(node_id, epoch)` остаётся за вызывающим.
    Порядок один для всех причин: таймеры фиксируются событием до оценки,
    журнал пишется до оценки, рассылка — после, `ScoreReady` — последним.
    """
    if state.ended:
        return
    ended_at = state.end(reason)
    if state.exercise is Exercise.CARD and state.dispatched_card is None:
        state.on_event("card.end")
    # Норматив отработки фиксируется событием, а не текущим значением часов.
    state.desk.finish_work()

    if state.voice is not None:
        await state.voice.close()
    hub.stop_ticker(session_id)

    hub.record(session_id, LessonEnded(ended_at, reason.value))
    await finish(session_id, state)

    hub.to_observers(session_id, SessionEnded(reason=reason))
    on_station = _ends_on_station(state)
    if on_station:
        hub.to_station(session_id, SessionEnded(reason=reason))
    else:
        hub.to_trainee(session_id, CallEnded(reason=reason))
    if state.score is None:
        return
    hub.to_observers(session_id, ScoreReady(session_id=session_id))
    await release_score(session_id, state)
    if on_station:
        hub.to_station(session_id, ScoreReady(session_id=session_id))


async def refresh_archived_report(session_id: UUID, state) -> None:
    """Дополнить сохранённый разбор самооценкой, пришедшей после hangup."""
    if state.score is None:
        return
    scenario = scoring_scenario(state)
    if scenario is None:
        return
    state.score["full_report"] = build_report(session_id, state, scenario).model_dump(mode="json")
    hub.record(session_id, ScoreArchived(state.score))


async def release_score(session_id: UUID, state) -> None:
    """Отдать оценку курсанту, когда самооценка сдана."""
    if state.score is not None and (state.self_assessed or state.exercise is Exercise.CARD):
        hub.to_trainee(session_id, ScoreReady(session_id=session_id))


def override_score(state, override: ScoreOverridden) -> None:
    """Правка балла преподавателем — одна операция для HTTP и WS.

    Вызывается внутри `hub.operation`: строка `Score`, архивный разбор и аудит
    пишутся тем же commit, что и снимок с исправленной оценкой.
    """
    hub.record(state.session_id, override)
    state.score = apply_score_override(state.score, override)
    hub.to_observers(state.session_id, ScoreReady(session_id=state.session_id))
