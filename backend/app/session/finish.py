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

from app.domain.events import CallEnded, CallEndReason, Exercise, ScoreReady, SessionEnded, SessionPaused
from app.domain.timers import TimerCode
from app.scoring.adjacent import adjacent_metrics
from app.scoring.ai_coach import coach
from app.scoring.dispatcher import dispatcher_metrics, evaluate_dispatcher
from app.scoring.gost import GostResult
from app.scoring.grammar import assess
from app.scoring.report import build as build_report
from app.scoring.timing import DDS_WORK_TIME, time_metric
from app.scoring.weights import apply_weights
from app.session.hub import hub
from app.session.score import ScoringSnapshot, score_session, scoring_scenario
from app.session.state import DdsCardRecord, DdsLiveCard, now_utc
from app.session.store import (
    FindingAdded,
    FindingReviewed,
    LessonEnded,
    LessonResumed,
    ScoreArchived,
    ScoreCalculated,
    ScoreOverridden,
    apply_finding_change,
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
    reaction_ms = state.criteria.dds_report_reaction_limit_seconds * 1000
    findings = evaluate_dispatcher(
        entries=card.status_log,
        services=services,
        crew_assignments=card.crew_assignments,
        deadline_ms=decision_limit_ms,
        elapsed_ms=card.timers.measured_ms(TimerCode.DDS_ACK),
        reply_text=card.reply_text,
        expected_decision=dds_decision.expected,
        expected_decision_reason=dds_decision.reason,
        phone_reports=card.phone_reports,
        report_reaction_ms=reaction_ms,
        report_reaction_enabled=state.criteria.dds_report_reaction_enabled,
        pause_intervals=state.pause_intervals,
    )
    metrics = dispatcher_metrics(
        card, decision_limit_ms, dds_decision.expected, dds_decision.reason,
        services=services, report_reaction_ms=reaction_ms,
        report_reaction_enabled=state.criteria.dds_report_reaction_enabled,
        pause_intervals=state.pause_intervals,
    )
    weighted = GostResult(metrics=metrics, findings=findings)
    work_limit_ms = card.timers.limits[TimerCode.DDS_WORK]
    work_timer = card.timers.timers.get(TimerCode.DDS_WORK)
    work_elapsed_ms = card.timers.measured_ms(TimerCode.DDS_WORK)
    if work_elapsed_ms is None and work_timer is not None and work_timer.started_at is not None:
        work_elapsed_ms = work_timer.current_ms(time.monotonic())
    # 3 минуты — на открытие и первую запись (статус с текстом), а не на
    # закрытие: работы по карточке могут идти часы и дни (ответ заказчика, П.5).
    recorded = any(mark.comment.strip() for mark in card.status_log if mark.service in services)
    work_metric, work_finding = time_metric(
        DDS_WORK_TIME, work_elapsed_ms, work_limit_ms, completed=recorded
    )
    weighted.metrics.append(work_metric)
    if work_finding is not None:
        weighted.findings.append(work_finding)
    extra_metrics, extra_findings = adjacent_metrics(
        card, state.criteria.dds_adjacent_limit_seconds * 1000, state.pause_intervals
    )
    weighted.metrics.extend(extra_metrics)
    weighted.findings.extend(extra_findings)
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
        metrics=[metric.model_copy(update={"title": f"Карточка {number}: {metric.title}",
                                           "card": number})
                 for metric in weighted.metrics],
        findings=[finding.model_copy(update={"summary": f"Карточка {number}: {finding.summary}",
                                             "card": number})
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
    # Все карточки выданы одновременно: при досрочном завершении оцениваем
    # каждую, включая не открытую, потому что её норматив уже шёл.
    if state.exercise is Exercise.DDS or (state.handoff_to_dds and state.desk.scenarios):
        score_live_dds_cards(state)
    # Сценарий занятия, а не библиотечный: директивы могли поправить эталон.
    snapshot = ScoringSnapshot.of(state)
    if snapshot is None:
        return
    report = await score_session(snapshot, grammar=assess)
    state.score = report.payload(card_status=state.station_snapshot().card.value)
    # Модель только поясняет уже посчитанные провалы и балл не трогает; без
    # запущенной модели разбор выходит со статусом «недоступно», а не ждёт её.
    state.score["ai_coaching"] = (await coach(report.result.metrics)).model_dump(mode="json")
    # Полный разбор хранится вместе с оценкой: PDF/CSV и история должны
    # переживать перезапуск backend, а не зависеть от живого объекта в реестре хаба.
    state.score["full_report"] = build_report(
        session_id, state, snapshot.scenario
    ).model_dump(mode="json")
    log.info("сессия %s: оценка %.1f, отметок %d",
             session_id, report.result.score, len(report.result.findings))

    # Оценка и аудит уходят в commit операции, завершившей занятие; ScoreReady
    # рассылает `end_session` и ждёт того же commit — без записи итог не выдаётся.
    hub.record(session_id, ScoreCalculated(report.result.score, state.score))


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
    was_paused = state.paused
    paused_ms_before = state.total_paused_ms
    ended_at = state.end(reason)
    if was_paused:
        hub.record(session_id, LessonResumed(
            at=ended_at, author="система", role="system",
            paused_ms=state.total_paused_ms - paused_ms_before,
        ))
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
    if was_paused:
        # Снятие паузы — после сообщения о конце: иначе клиент дошлёт отложенные
        # на паузе правки карточки в уже закрытое занятие.
        hub.broadcast(session_id, SessionPaused(paused=False))
        hub.to_station(session_id, SessionPaused(paused=False))
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


def change_findings(state, change: FindingReviewed | FindingAdded) -> None:
    """Решение преподавателя по отметке — тем же путём, что правка итога.

    Вызывается внутри `hub.operation`: отметки, пересчитанный балл, архивный
    разбор и строка аудита уходят одним commit.
    """
    state.score = apply_finding_change(state.score, change)
    hub.record(state.session_id, change)
    hub.to_observers(state.session_id, ScoreReady(session_id=state.session_id))
