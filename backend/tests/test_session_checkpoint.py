"""Промежуточное состояние занятия переживает смену backend-процесса."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from app.domain.events import (
    Exercise,
    LessonCriteria,
    Metric,
    SessionMode,
)
from app.domain.kio import KIO
from app.domain.statuses import PhoneCallPending, PhoneReportRecord, ServiceStatus
from app.domain.taxonomy import ErrorCode, Finding, FindingSource
from app.domain.timers import TimerCode
from app.scenarios.loader import load_file
from app.session.checkpoint import dump_state, load_state
from app.session.dds import prepare_handoff_queue, prepare_queue
from app.session.finish import score_dds_card
from app.session.state import DdsCardRecord, SessionState, now_utc

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


def dds_state() -> SessionState:
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(),
        scenario_id=scenario.id,
        scenario_title=scenario.title,
        level=scenario.level.value,
        mode=SessionMode.TRAINING,
        exercise=Exercise.DDS,
        scenario=scenario.model_copy(deep=True),
        required_fields=list(scenario.required_fields),
        trainee_name="Курсант для восстановления",
        attempt=2,
        criteria=LessonCriteria(
            decision_time_limit_seconds=45,
            allowed_errors=1,
            require_correct_grammar=True,
        ),
    )
    state.timers.limits[TimerCode.DDS_ACK] = 45_000
    prepare_queue(state, [scenario.model_copy(deep=True)])
    card = state.desk.active
    service = state.card_services(card)[0]
    card.set_service_status(service, ServiceStatus.ACCEPTED, "Принято в работу", author="диспетчер")
    card.crew_selected = state.crew_options(card)[0]
    card.crew_assignments[service] = card.crew_selected
    card.phone_pending = PhoneCallPending(
        service=service, crew=card.crew_selected, phase="dispatched"
    )
    card.reply_text = "Сообщение принято, бригада направлена."
    card.reply_log.append((now_utc(), card.reply_text))
    card.dds_log.append(("crew.select", now_utc(), card.crew_selected))
    return state


def test_active_dds_session_round_trips_without_losing_work():
    before = dds_state()
    before.processed_station_commands = ["2a831a63-dbb0-4d9f-af5b-21a617520001"]
    payload = dump_state(before)
    restored = load_state(
        payload,
        datetime.now(UTC) - timedelta(seconds=2),
    )

    assert restored.session_id == before.session_id
    assert restored.exercise is Exercise.DDS
    assert restored.criteria.decision_time_limit_seconds == 45
    card, card_before = restored.desk.active, before.desk.active
    assert card.dispatched_card == card_before.dispatched_card
    assert card.status_log == card_before.status_log
    assert card.crew_selected == card_before.crew_selected
    assert card.crew_assignments == card_before.crew_assignments
    assert card.phone_pending == card_before.phone_pending
    assert card.reply_text == card_before.reply_text
    assert restored.processed_station_commands == before.processed_station_commands
    assert restored.desk.scenarios[0].id == before.scenario_id
    # Время простоя backend входит в норматив, а не обнуляет таймер.
    timer = next(item for item in card.timers.snapshot() if item.code is TimerCode.DDS_ACK)
    assert timer.elapsed_ms >= 1_900
    assert timer.limit_ms == 45_000


def test_report_reaction_keeps_times_and_limit_after_restore():
    before = dds_state()
    before.criteria.dds_report_reaction_enabled = True
    before.criteria.dds_report_reaction_limit_seconds = 20
    card = before.desk.active
    service = before.card_services(card)[0]
    card.phone_reports.append(PhoneReportRecord(
        service=service, crew=card.crew_selected, phase="dispatched",
        text="Бригада выехала", at=now_utc(),
    ))
    card.set_service_status(service, ServiceStatus.RESPONDING,
                            "Основание: доклад.\nСведения: бригада выехала.")
    card.phone_reports[0].at -= timedelta(seconds=70)

    restored = load_state(dump_state(before), now_utc())

    assert restored.criteria.dds_report_reaction_limit_seconds == 20
    assert restored.criteria.dds_report_reaction_enabled
    assert restored.desk.active.phone_reports == card.phone_reports

    def reaction(state):
        result = score_dds_card(state, state.desk.active)
        metric = next(item for item in result.metrics if item.key == "dds_report_reaction")
        findings = [item for item in result.findings if item.metric_key == "dds_report_reaction"]
        return metric, findings

    metric, findings = reaction(restored)
    assert not metric.passed
    assert metric.norm == "отметить статус по докладу за 20 с"
    assert "70 с (+50 с сверх норматива)" in metric.fact
    assert [item.code for item in findings] == [ErrorCode.D6]
    assert reaction(restored) == reaction(before), "оценка после восстановления та же"


def test_report_reaction_default_off_keeps_status_completeness_d6():
    state = dds_state()
    card = state.desk.active
    service = state.card_services(card)[0]
    card.set_service_status(service, ServiceStatus.RESPONDING,
                            "Основание: доклад.\nСведения: бригада выехала.")
    without_report = state.model_copy(deep=True)
    card.phone_reports.append(PhoneReportRecord(
        service=service, crew=card.crew_selected, phase="dispatched",
        text="Бригада выехала", at=card.status_log[-1].at - timedelta(seconds=70),
    ))

    result = score_dds_card(state, card)
    baseline = score_dds_card(without_report, without_report.desk.active)

    assert state.criteria.dds_report_reaction_enabled is False
    assert result.score_auto == baseline.score_auto
    assert [(item.code, item.metric_key, item.fact) for item in result.findings] == [
        (item.code, item.metric_key, item.fact) for item in baseline.findings
    ]
    assert not any(item.key == "dds_report_reaction" for item in result.metrics)
    assert not any(item.metric_key == "dds_report_reaction" for item in result.findings)
    assert any(item.code is ErrorCode.D6 for item in result.findings)


def completed_card() -> DdsCardRecord:
    return DdsCardRecord(
        card_id=uuid4(),
        scenario_id="fire-apartment-l2",
        reply_text="Бригада направлена.",
        metrics=[Metric(key="dds_ack", title="Приём", fact="20 с", norm="30 с", passed=True)],
        findings=[Finding(code=ErrorCode.D1, source=FindingSource.DISPATCHER,
                          summary="Поздний статус", fact="статус через 5 мин")],
        actions=[{"action": "crew.select", "detail": "АЦ-1"}],
        duration_ms=61_000,
        title="Пожар в квартире",
        address="улица Ленина, 14, кв. 5",
        description="дым из окна",
        incident_type="пожар",
        victims_count=2,
        received_at=datetime(2026, 9, 26, 10, 15, 30, 123000, tzinfo=UTC),
        managed_service="01",
        recipient_services=["01", "03"],
    )


def test_text_revealed_facts_and_completed_cards_round_trip_whole():
    before = dds_state()
    before.text_revealed_facts = {"address": "улица Ленина, 14", "floor": "5 этаж, уточнено"}
    before.desk.completed = [completed_card()]

    restored = load_state(dump_state(before), now_utc())

    assert restored.text_revealed_facts == before.text_revealed_facts
    assert restored.desk.completed == before.desk.completed
    assert restored.desk.completed[0].received_at.tzinfo is not None


def test_checkpoint_rejects_unknown_format_version():
    payload = dump_state(dds_state())
    payload["version"] = 999
    with pytest.raises(ValueError, match="версия"):
        load_state(payload, datetime.now(UTC))


def test_concurrent_dds_queue_round_trips_with_each_timer_and_status():
    first = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    second = first.model_copy(
        deep=True,
        update={"id": "checkpoint-second", "title": "Вторая карточка восстановления"},
    )
    state = SessionState(
        session_id=uuid4(),
        scenario_id=first.id,
        scenario_title=first.title,
        level=first.level.value,
        mode=SessionMode.TRAINING,
        exercise=Exercise.DDS,
    )
    prepare_queue(state, [first, second])
    first_card = state.desk.active
    first_id = first_card.card_id
    first_service = state.card_services(first_card)[0]
    first_card.set_service_status(first_service, ServiceStatus.ACCEPTED, "Принято в работу")
    first_card.on_event("card.ack")
    second_id = state.desk.ordered()[1].card_id
    assert state.desk.open(second_id)

    restored = load_state(
        dump_state(state),
        datetime.now(UTC) - timedelta(seconds=2),
    )

    assert restored.desk.active_id == second_id
    assert len(restored.desk.cards) == 2
    first_restored = restored.desk.cards[first_id]
    second_restored = restored.desk.cards[second_id]
    assert first_restored.status_log[-1].status is ServiceStatus.ACCEPTED
    assert first_restored.timers.measured_ms(TimerCode.DDS_ACK) is not None
    second_timer = next(
        item for item in second_restored.timers.snapshot() if item.code is TimerCode.DDS_ACK
    )
    assert second_timer.elapsed_ms >= 1_900
    assert second_timer.stopped is False


def test_delivering_next_dds_card_does_not_clear_previous_card_state():
    first = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    scenarios = [
        first.model_copy(
            deep=True,
            update={"id": f"scheduled-checkpoint-{index}", "title": f"Карточка {index}"},
        )
        for index in range(3)
    ]
    state = SessionState(
        session_id=uuid4(),
        scenario_id=scenarios[0].id,
        scenario_title=scenarios[0].title,
        level=scenarios[0].level.value,
        mode=SessionMode.TRAINING,
        exercise=Exercise.DDS,
    )
    prepare_queue(state, scenarios, arrival_interval_seconds=60, max_waiting=1)
    first = state.desk.active
    first_id = first.card_id
    service = state.card_services(first)[0]
    first.set_service_status(service, ServiceStatus.ACCEPTED, "Принято в работу")

    assert state.desk.deliver_due(now_utc() + timedelta(seconds=61)) == 1
    assert state.desk.cards[first_id].status_log[-1].status is ServiceStatus.ACCEPTED
    second = next(card for card in state.desk.ordered() if card.original_index == 1)
    assert state.desk.open(second.card_id)

    restored = load_state(dump_state(state), now_utc())
    first_restored = restored.desk.cards[first_id]
    assert first_restored.status_log[-1].status is ServiceStatus.ACCEPTED
    assert restored.desk.active_id == second.card_id
    assert len(restored.desk.cards) == 2
    assert restored.desk.next_index == 2
    assert restored.desk.next_arrival_at is not None


def test_mixed_handoff_checkpoint_preserves_operator_card_and_generated_queue():
    first = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    second = load_file(LIBRARY / "tickets" / "t01-1-fire-container.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=first.id, scenario_title=first.title,
        level=first.level.value, mode=SessionMode.TRAINING,
        exercise=Exercise.CARD, handoff_to_dds=True, scenario=first,
        pending_dds_scenarios=[second],
    )
    state.kio = KIO(address="улица Ленина, 14", description="горит балкон")
    state.dispatch()
    prepare_handoff_queue(state, state.pending_dds_scenarios)

    restored = load_state(dump_state(state), now_utc())

    assert restored.operator_kio.address == "улица Ленина, 14"
    assert restored.operator_scenario.id == first.id
    assert restored.desk.scenarios[0].id == first.id
    assert restored.desk.scenarios[1].id == second.id
    assert len(restored.desk.cards) == 2
    assert restored.desk.active_id == state.desk.active_id
