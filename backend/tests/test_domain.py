"""Тесты контракта. Домен — общий шов, ломать его молча нельзя."""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain.events import (
    EventCatalog,
    KioPatchIn,
    ServerToTrainee,
    SessionMode,
    TraineeToServer,
)
from app.domain.kio import KIO, apply_patch, missing_fields
from app.domain.timers import NORMATIVES, TimerCode, TimerState, state_for
from scripts.export_types import OUT, render


def test_interview_normative_is_75_seconds():
    """Центральная метрика продукта. Значение меняется только вместе с ГОСТ."""
    assert NORMATIVES[TimerCode.INTERVIEW].limit_ms == 75_000


@pytest.mark.parametrize(
    "elapsed_ms,expected",
    [(0, TimerState.OK), (59_000, TimerState.OK), (70_000, TimerState.WARN), (94_000, TimerState.VIOLATED)],
)
def test_timer_state(elapsed_ms, expected):
    assert state_for(elapsed_ms, 75_000) is expected


def test_patch_applies_nested_and_ignores_service_fields():
    card = KIO(caller_number="+7 999 000-00-00")
    patched = apply_patch(card, {"floor": "5", "fire.floors": 9, "caller_number": "подмена"})

    assert patched.floor == "5"
    assert patched.fire is not None and patched.fire.floors == 9
    # Номер определяется автоматически и курсантом не редактируется.
    assert patched.caller_number == "+7 999 000-00-00"


def test_missing_fields_reports_required_only():
    card = KIO(address="улица Ленина, 14")
    assert missing_fields(card, ["address", "floor", "victims_count"]) == ["floor", "victims_count"]


def test_trainee_events_are_discriminated_by_type():
    adapter = TypeAdapter(TraineeToServer)
    event = adapter.validate_python({"type": "kio.patch", "fields": {"floor": "5"}})
    assert isinstance(event, KioPatchIn)

    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "kio.state", "kio": {}})


def test_server_events_round_trip_through_json():
    adapter = TypeAdapter(ServerToTrainee)
    payload = {
        "type": "caller.utterance",
        "utterance_id": str(uuid4()),
        "text": "Алло! Помогите! Горим!",
        "at": datetime.now(timezone.utc).isoformat(),
        "mood": "panic",
    }
    assert adapter.validate_python(payload).text.startswith("Алло")


def test_session_modes_are_the_three_from_docs():
    assert {mode.value for mode in SessionMode} == {"training", "exam", "self"}


def test_event_catalog_covers_every_channel():
    assert set(EventCatalog.model_fields) == {
        "server_to_trainee",
        "trainee_to_server",
        "server_to_observer",
        "instructor_to_server",
        "server_to_station",
        "station_to_server",
        "session_report",
    }


def test_generated_types_match_models():
    """Забытый `make types` ловится здесь, а не на фронте в последнюю ночь."""
    assert OUT.exists(), "нет frontend/src/shared/types/generated.ts — запусти make types"
    assert OUT.read_text(encoding="utf-8") == render(), "generated.ts устарел — запусти make types"
