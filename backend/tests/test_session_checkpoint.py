"""Промежуточное состояние занятия переживает смену backend-процесса."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.events import Exercise, LessonCriteria, SessionMode
from app.domain.statuses import PhoneCallPending, ServiceStatus
from app.domain.timers import TimerCode
from app.scenarios.loader import load_file
from app.scoring.grammar import basic_check
from app.session.checkpoint import dump_state, load_state
from app.session.dds import deliver_due_cards, prepare_queue
from app.session.state import SessionState, now_utc

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
        dds_scenarios=[scenario.model_copy(deep=True)],
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
    prepare_queue(state, state.dds_scenarios)
    service = state.notified_services()[0]
    state.set_service_status(service, ServiceStatus.ACCEPTED, author="диспетчер")
    state.crew_selected = state.crew_options()[0]
    state.crew_assignments[service] = state.crew_selected
    state.phone_pending = PhoneCallPending(
        service=service, crew=state.crew_selected, phase="dispatched"
    )
    state.reply_text = "Сообщение принято, бригада направлена."
    state.reply_grammar = basic_check(state.reply_text)
    state.reply_log.append((now_utc(), state.reply_text))
    state.dds_log.append(("crew.select", now_utc(), state.crew_selected))
    return state


def test_active_dds_session_round_trips_without_losing_work():
    before = dds_state()
    payload = dump_state(before)
    restored = load_state(
        payload,
        datetime.now(UTC) - timedelta(seconds=2),
    )

    assert restored.session_id == before.session_id
    assert restored.exercise is Exercise.DDS
    assert restored.criteria.decision_time_limit_seconds == 45
    assert restored.dispatched_card == before.dispatched_card
    assert restored.status_log == before.status_log
    assert restored.crew_selected == before.crew_selected
    assert restored.crew_assignments == before.crew_assignments
    assert restored.phone_pending == before.phone_pending
    assert restored.reply_text == before.reply_text
    assert restored.reply_grammar == before.reply_grammar
    assert restored.dds_scenarios[0].id == before.scenario_id
    # Время простоя backend входит в норматив, а не обнуляет таймер.
    timer = next(item for item in restored.timers.snapshot() if item.code is TimerCode.DDS_ACK)
    assert timer.elapsed_ms >= 1_900
    assert timer.limit_ms == 45_000


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
        dds_scenarios=[first, second],
    )
    prepare_queue(state, state.dds_scenarios)
    first_id = state.dispatched_card.card_id
    first_service = state.managed_services()[0]
    state.set_service_status(first_service, ServiceStatus.ACCEPTED)
    state.on_event("card.ack")
    second_id = state.dds_live_cards[1].card_id
    assert state.activate_dds_card(second_id)

    restored = load_state(
        dump_state(state),
        datetime.now(UTC) - timedelta(seconds=2),
    )

    assert restored.dds_active_card_id == second_id
    assert restored.dispatched_card.card_id == second_id
    assert len(restored.dds_live_cards) == 2
    first_restored = next(item for item in restored.dds_live_cards if item.card_id == first_id)
    second_restored = next(item for item in restored.dds_live_cards if item.card_id == second_id)
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
        dds_scenarios=scenarios,
    )
    prepare_queue(state, scenarios, arrival_interval_seconds=60, max_waiting=1)
    first_id = state.dds_live_cards[0].card_id
    service = state.managed_services()[0]
    state.set_service_status(service, ServiceStatus.ACCEPTED)
    state.capture_active_dds()

    assert deliver_due_cards(state, now_utc() + timedelta(seconds=61)) == 1
    first = next(card for card in state.dds_live_cards if card.card_id == first_id)
    assert first.status_log[-1].status is ServiceStatus.ACCEPTED
    second = next(card for card in state.dds_live_cards if card.original_index == 1)
    assert state.activate_dds_card(second.card_id)

    restored = load_state(dump_state(state), now_utc())
    first_restored = next(card for card in restored.dds_live_cards if card.card_id == first_id)
    assert first_restored.status_log[-1].status is ServiceStatus.ACCEPTED
    assert restored.dds_active_card_id == second.card_id
    assert len(restored.dds_live_cards) == 2
    assert restored.dds_next_scenario_index == 2
    assert restored.dds_next_arrival_at is not None
