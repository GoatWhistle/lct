"""Прогресс звонящего — чистые данные, из которых собирается живой звонящий.

В снимок занятия прогресс пока не пишется; модель заложена, чтобы включение
сохранения было одним полем сохраняемой части и передачей в пересборку.
"""

from uuid import uuid4

from app.dialog.persona import PersonaProgress, PersonaState
from app.dialog.slots import SlotMachine, SlotProgress
from app.domain.events import Exercise, Mood, SessionMode
from app.session import checkpoint
from app.session.checkpoint import CallerProgress, dump_state, rebuild_live
from app.session.state import SessionState
from tests.test_refinement import SCENARIO
from tests.test_slots import StemEmbedder


def slot_progress() -> SlotProgress:
    return SlotProgress(asked=["q_address", "q_city"], revealed=["f_address"],
                        refined=["f_address"])


def persona_progress() -> PersonaProgress:
    return PersonaProgress(stage="dispatch", repeats=2, directive="panic_rises",
                           history=[Mood.CALM, Mood.PANIC])


def test_slot_machine_resumes_given_progress():
    progress = slot_progress()

    slots = SlotMachine(SCENARIO, StemEmbedder(), progress=progress)

    assert slots.progress == progress
    assert [fact.id for fact in slots.revealed_facts()] == ["f_address"]
    assert slots.refined == ["f_address"]
    assert slots.value_of("f_address") == "Королёв, улица Станционная, дом 28"
    assert [item.id for item in slots.unasked()] == []


def test_slot_machine_without_progress_starts_clean():
    slots = SlotMachine(SCENARIO, StemEmbedder())

    assert slots.progress == SlotProgress()
    slots.hear("Назовите адрес")
    assert slots.progress.revealed == ["f_address"]


def test_persona_resumes_given_progress():
    progress = persona_progress()

    persona = PersonaState(SCENARIO.persona, progress=progress)

    assert persona.progress == progress
    assert persona.mood is Mood.PANIC
    assert persona.repeats == 2


def call_state() -> SessionState:
    return SessionState(
        session_id=uuid4(), scenario_id=SCENARIO.id, scenario_title=SCENARIO.title,
        level=SCENARIO.level.value, mode=SessionMode.TRAINING,
        exercise=Exercise.CALL, scenario=SCENARIO,
    )


def test_rebuild_live_uses_caller_progress(monkeypatch):
    monkeypatch.setattr(checkpoint, "get_embedder", StemEmbedder)
    state = call_state()
    progress = CallerProgress(slots=slot_progress(), persona=persona_progress())

    rebuild_live(state, progress)

    assert state.slots.progress == progress.slots
    assert state.persona.progress == progress.persona
    assert state.caller is not None


def test_rebuild_live_without_progress_starts_clean(monkeypatch):
    monkeypatch.setattr(checkpoint, "get_embedder", StemEmbedder)
    state = call_state()

    rebuild_live(state)

    assert state.slots.progress == SlotProgress()
    assert state.persona.progress == PersonaProgress()


def test_snapshot_has_no_caller_progress(monkeypatch):
    monkeypatch.setattr(checkpoint, "get_embedder", StemEmbedder)
    state = call_state()
    rebuild_live(state, CallerProgress(slots=slot_progress(), persona=persona_progress()))

    payload = dump_state(state)

    assert not {"slots", "persona", "caller", "caller_progress"} & set(payload)
    assert "panic_rises" not in str(payload)
