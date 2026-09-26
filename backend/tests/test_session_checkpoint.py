"""Промежуточное состояние занятия переживает смену backend-процесса."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from app.domain.events import (
    CallStarted,
    CommandAck,
    Exercise,
    LessonCriteria,
    Metric,
    SessionMode,
)
from app.domain.kio import KIO
from app.domain.statuses import PhoneCallPending, ServiceStatus
from app.domain.taxonomy import ErrorCode, Finding, FindingSource
from app.domain.timers import TimerCode
from app.scenarios.loader import load_file
from app.session.checkpoint import dump_state, load_state
from app.session.dds import deliver_due_cards, prepare_handoff_queue, prepare_queue
from app.session.hub import LEASE_FENCED_MESSAGE, SessionHub
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
    state.set_service_status(service, ServiceStatus.ACCEPTED, "Принято в работу", author="диспетчер")
    state.crew_selected = state.crew_options()[0]
    state.crew_assignments[service] = state.crew_selected
    state.phone_pending = PhoneCallPending(
        service=service, crew=state.crew_selected, phase="dispatched"
    )
    state.reply_text = "Сообщение принято, бригада направлена."
    state.reply_log.append((now_utc(), state.reply_text))
    state.dds_log.append(("crew.select", now_utc(), state.crew_selected))
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
    assert restored.dispatched_card == before.dispatched_card
    assert restored.status_log == before.status_log
    assert restored.crew_selected == before.crew_selected
    assert restored.crew_assignments == before.crew_assignments
    assert restored.phone_pending == before.phone_pending
    assert restored.reply_text == before.reply_text
    assert restored.processed_station_commands == before.processed_station_commands
    assert restored.dds_scenarios[0].id == before.scenario_id
    # Время простоя backend входит в норматив, а не обнуляет таймер.
    timer = next(item for item in restored.timers.snapshot() if item.code is TimerCode.DDS_ACK)
    assert timer.elapsed_ms >= 1_900
    assert timer.limit_ms == 45_000


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
    before.dds_completed = [completed_card()]

    restored = load_state(dump_state(before), now_utc())

    assert restored.text_revealed_facts == before.text_revealed_facts
    assert restored.dds_completed == before.dds_completed
    assert restored.dds_completed[0].received_at.tzinfo is not None


def test_legacy_checkpoint_without_new_keys_gets_defaults():
    before = dds_state()
    before.text_revealed_facts = {"address": "улица Ленина, 14"}
    before.dds_completed = [completed_card()]
    payload = dump_state(before)
    del payload["text_revealed_facts"]
    legacy_keys = {"card_id", "scenario_id", "reply_text", "metrics",
                   "findings", "actions", "duration_ms"}
    payload["dds_completed"] = [
        {key: value for key, value in item.items() if key in legacy_keys}
        for item in payload["dds_completed"]
    ]

    restored = load_state(payload, now_utc())

    assert restored.text_revealed_facts == {}
    card = restored.dds_completed[0]
    assert card.card_id == before.dds_completed[0].card_id
    assert (card.title, card.address, card.description, card.incident_type,
            card.victims_count, card.received_at, card.managed_service) == (None,) * 7
    assert card.recipient_services == []


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
    state.set_service_status(first_service, ServiceStatus.ACCEPTED, "Принято в работу")
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
    state.set_service_status(service, ServiceStatus.ACCEPTED, "Принято в работу")
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
    assert restored.dds_scenarios[0].id == first.id
    assert restored.dds_scenarios[1].id == second.id
    assert len(restored.dds_live_cards) == 2
    assert restored.dds_active_card_id == state.dds_active_card_id


def test_checkpoint_storage_failure_fences_and_notifies_all_data_channels():
    class BrokenJournal:
        async def checkpoint(self, _state):
            raise OSError("simulated database partition")

    local_hub = SessionHub(journal=BrokenJournal())
    state = dds_state()
    local_hub.register(state)

    with local_hub.observer(state.session_id) as observers, \
         local_hub.trainee(state.session_id) as trainee, \
         local_hub.station(state.session_id) as station:
        async def failing_transition():
            async with local_hub.durable_transition(state.session_id):
                local_hub.to_trainee(
                    state.session_id, CallStarted(started_at=now_utc())
                )
                assert trainee.empty(), "success event escaped before durable checkpoint"

        with pytest.raises(OSError, match="partition"):
            asyncio.run(failing_transition())

        assert state.lease_fenced
        assert local_hub.get(state.session_id) is None
        for queue in (observers, trainee, station):
            event = queue.get_nowait()
            assert event.message == LEASE_FENCED_MESSAGE
            assert queue.empty(), "uncommitted success event leaked during fencing"


def test_durable_transition_publishes_event_only_after_checkpoint_commit():
    class CommitJournal:
        committed = False

        async def checkpoint(self, _state):
            await asyncio.sleep(0)
            self.committed = True

    journal = CommitJournal()
    local_hub = SessionHub(journal=journal)
    state = dds_state()
    local_hub.register(state)

    command_id = uuid4()
    with local_hub.trainee(state.session_id) as trainee, \
         local_hub.station(state.session_id) as station:
        async def transition():
            async with local_hub.durable_transition(state.session_id):
                local_hub.to_trainee(
                    state.session_id, CallStarted(started_at=now_utc())
                )
                local_hub.to_station(
                    state.session_id, CommandAck(command_id=command_id)
                )
                assert trainee.empty()
                assert station.empty()
            assert journal.committed

        asyncio.run(transition())
        event = trainee.get_nowait()
        assert isinstance(event, CallStarted)
        ack = station.get_nowait()
        assert isinstance(ack, CommandAck)
        assert ack.command_id == command_id
