"""Снимок занятия — это модель сохраняемой части сессии, а не список ключей.

Фикстура заполняет каждое поле значением не по умолчанию: новое поле, которое
не пережило round-trip или осталось дефолтным в фикстуре, роняет тест.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel

from app.domain.events import (
    CallEndReason,
    Exercise,
    LessonCriteria,
    Metric,
    SessionMode,
    Speaker,
    TranscriptEntry,
)
from app.domain.kio import KIO
from app.domain.statuses import (
    PhoneCallPending,
    PhoneLineRecord,
    PhoneReportRecord,
    ServiceStatus,
    StatusEntry,
)
from app.domain.taxonomy import ErrorCode, Finding, FindingSource
from app.domain.timers import TimerCode
from app.scenarios.loader import load_file
from app.session import timers as timers_module
from app.session.checkpoint import CHECKPOINT_VERSION, dump_state, load_state
from app.session.dds import DdsCardRecord, DdsDesk, DdsLiveCard
from app.session.state import PersistedSession, SessionState
from app.session.timers import SessionTimers, Timer

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"

MONO = 1_000_000.0
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
AT = NOW - timedelta(minutes=5)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    """Часы процесса и стены заморожены: round-trip таймера сравнивается точно."""
    monkeypatch.setattr(timers_module.time, "monotonic", lambda: MONO)
    monkeypatch.setattr(timers_module, "now_utc", lambda: NOW)


def timers() -> SessionTimers:
    limits = {**SessionTimers().limits, TimerCode.DDS_ACK: 45_000}
    return SessionTimers(limits=limits, timers={
        TimerCode.DDS_ACK: Timer(code=TimerCode.DDS_ACK, started_at=MONO - 12.0, attempt=2),
        TimerCode.ANSWER: Timer(code=TimerCode.ANSWER, started_at=MONO - 7.0,
                                elapsed_ms=7_000, attempt=3, stopped=True),
    })


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
        received_at=AT,
        managed_service="01",
        recipient_services=["01", "03"],
    )


def full_state() -> SessionState:
    first = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    second = load_file(LIBRARY / "tickets" / "t01-1-fire-container.yaml", LIBRARY)
    crew = "01 — бригада №1"
    card = DdsLiveCard(
        original_index=1,
        scenario=second,
        kio=KIO(address="улица Мира, 1", description="горит контейнер"),
        dispatched_card=KIO(address="улица Мира, 1", description="горит"),
        dispatched_at=AT,
        timers=timers(),
        bounced_fields=["address"],
        dds_log=[("crew.select", AT, crew), ("crew.dispatched", AT, None)],
        status_log=[StatusEntry(service="01", status=ServiceStatus.ACCEPTED, at=AT,
                                comment="Принято в работу", author="диспетчер")],
        crew_selected=crew,
        crew_assignments={"01": crew},
        phone_reports=[PhoneReportRecord(service="01", crew=crew, phase="dispatched",
                                         text="Выехали", at=AT)],
        phone_lines=[PhoneLineRecord(service="01", crew=crew, speaker="crew",
                                     text="Выехали", at=AT)],
        phone_pending=PhoneCallPending(service="01", crew=crew, phase="arrived"),
        reply_text="Бригада направлена",
        reply_log=[(AT, "Бригада направлена")],
    )
    desk = DdsDesk(
        scenarios=[first, second],
        cards={card.card_id: card},
        active_id=card.card_id,
        card_index=1,
        completed=[completed_card()],
        limits={**SessionTimers().limits, TimerCode.DDS_ACK: 45_000},
        arrival_interval_seconds=60,
        max_waiting=2,
        next_index=2,
        next_arrival_at=NOW + timedelta(seconds=60),
    )
    return SessionState(
        session_id=uuid4(),
        scenario_id=first.id,
        scenario_title=first.title,
        level=first.level.value,
        mode=SessionMode.EXAM,
        owner_login="teacher",
        backend_fencing_epoch=7,
        exercise=Exercise.CARD,
        handoff_to_dds=True,
        required_fields=["address"],
        trainee_name="Курсант",
        trainee_id=uuid4(),
        dds_service="01",
        attempt=3,
        criteria=LessonCriteria(decision_time_limit_seconds=45, allowed_errors=1),
        kio=KIO(address="улица Ленина, 14"),
        transcript=[TranscriptEntry(ref="u1", speaker=Speaker.CALLER, text="Алло", at=AT)],
        timers=timers(),
        hints_shown=["q_address"],
        hints_log=[("q_address", AT)],
        notes=[{"ref": "u1", "text": "уверенно"}],
        directives=["panic_rises"],
        scenario=first,
        audio_frames=10,
        bad_frames=1,
        self_assessed=True,
        self_assessment={"score": 80},
        score={"score_auto": 75.0},
        started_at=AT,
        ended_at=NOW,
        end_reason=CallEndReason.COMPLETE,
        dispatched_card=KIO(address="улица Ленина, 14", description="горит балкон"),
        dispatched_at=AT,
        bounced_fields=["floor"],
        pending_dds_scenarios=[second],
        operator_kio=KIO(address="улица Ленина, 14"),
        operator_scenario=first,
        desk=desk,
        resolved_outcome="справка",
        resolve_comment="передано в другой регион",
        processed_station_commands=[str(uuid4())],
        text_revealed_facts={"f_address": "улица Ленина, 14"},
        consecutive_refusals=2,
    )


def test_persisted_part_round_trips_whole():
    before = full_state()

    restored = load_state(dump_state(before), NOW)

    assert restored.persisted() == before.persisted()


def _session_models(value, found: dict[type, list]) -> None:
    """Все экземпляры моделей сессии в сохраняемой части, по классам."""
    if isinstance(value, PersistedSession):
        found.setdefault(PersistedSession, []).append(value)
        for name in PersistedSession.model_fields:
            _session_models(getattr(value, name), found)
    elif isinstance(value, BaseModel):
        if type(value).__module__.startswith("app.session"):
            found.setdefault(type(value), []).append(value)
            for name in type(value).model_fields:
                _session_models(getattr(value, name), found)
    elif isinstance(value, dict):
        for item in value.values():
            _session_models(item, found)
    elif isinstance(value, list | tuple):
        for item in value:
            _session_models(item, found)


def test_fixture_sets_every_persisted_field_to_non_default():
    found: dict[type, list] = {}
    _session_models(full_state(), found)

    assert {PersistedSession, DdsDesk, DdsLiveCard, DdsCardRecord,
            SessionTimers, Timer} <= set(found)
    for model, instances in found.items():
        for name, info in model.model_fields.items():
            if info.is_required():
                continue
            default = info.get_default(call_default_factory=True)
            assert any(getattr(item, name) != default for item in instances), (
                f"{model.__name__}.{name} в фикстуре по умолчанию — round-trip его не проверяет"
            )


def test_running_timer_counts_downtime_stopped_keeps_saved_value():
    payload = dump_state(full_state())

    restored = load_state(payload, NOW - timedelta(seconds=5))

    items = restored.timers.timers
    assert items[TimerCode.DDS_ACK].current_ms(MONO) == 12_000 + 5_000
    assert items[TimerCode.ANSWER].current_ms(MONO) == 7_000
    card = restored.desk.active
    assert card.timers.timers[TimerCode.DDS_ACK].current_ms(MONO) == 17_000


def test_snapshot_does_not_carry_process_clock():
    timer = dump_state(full_state())["timers"]["timers"][TimerCode.DDS_ACK.value]

    assert "started_at" not in timer
    assert timer["elapsed_ms"] == 12_000


def test_checkpoint_version_1_is_rejected():
    payload = dump_state(full_state())
    assert payload["version"] == CHECKPOINT_VERSION == 2
    payload["version"] = 1

    with pytest.raises(ValueError, match="версия"):
        load_state(payload, NOW)


def test_dump_state_does_not_change_state():
    state = full_state()
    active = state.desk.active

    first = dump_state(state)

    assert dump_state(state) == first
    assert state.desk.active is active


def test_snapshot_keeps_only_persisted_keys():
    payload = dump_state(full_state())

    assert set(payload) == {"version", *PersistedSession.model_fields}
    assert not {"slots", "persona", "caller", "voice", "recorder",
                "recording_path", "lease_fenced"} & set(payload)


def test_processed_station_commands_are_trimmed_on_load():
    state = full_state()
    state.processed_station_commands = [str(index) for index in range(600)]

    restored = load_state(dump_state(state), NOW)

    assert restored.processed_station_commands == [str(index) for index in range(88, 600)]


def test_live_field_must_be_excluded_from_snapshot():
    with pytest.raises(TypeError, match="сохраняемое"):
        class Undecided(PersistedSession):
            forgotten: int = 0
