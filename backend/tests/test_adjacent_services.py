"""Смежные службы в карточке ДДС: таймлайн, вводная «бригада просит смежников»,
метрики dds_adjacent_check / dds_adjacent_call (issue 68)."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain.events import AdjacentDial, CardAck, Exercise, PhoneReply, SessionMode
from app.domain.statuses import (
    ADJACENT_AUTHOR,
    ADJACENT_CALL_AUTHOR,
    ServiceStatus,
)
from app.scenarios.loader import ScenarioError, load_file
from app.scoring.adjacent import adjacent_metrics
from app.session.dds import classify_reply, deliver_adjacent_due, prepare_queue
from app.session.state import SessionState
from app.session.timers import now_utc

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"
BASIS = "Основание: доклад старшего. Сведения: карточка принята."
REASSURE = "Мособлгаз отреагировал, аварийная бригада уже едет к вам"
DEFER = "Понял вас, уточню по карточке и свяжусь со смежной службой"
REQUEST = "Прошу обесточить и перекрыть газовый ввод по карточке, работает бригада"


def lesson(name: str) -> SessionState:
    scenario = load_file(LIBRARY / f"{name}.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
    )
    prepare_queue(state, [scenario])
    return state


def accepted(name: str) -> SessionState:
    state = lesson(name)
    assert state.desk.apply(CardAck(comment=BASIS), state).changed
    return state


def advance(state: SessionState, seconds: int) -> None:
    card = state.desk.active
    card.adjacent_started_at -= timedelta(seconds=seconds)
    if card.crew_request_at is not None:
        card.crew_request_at -= timedelta(seconds=seconds)
    deliver_adjacent_due(state)


# ── загрузка ──


def load_variant(tmp_path, patch: str, name: str = "broken.yaml"):
    source = (LIBRARY / "dds-adjacent-react.yaml").read_text(encoding="utf-8")
    (tmp_path / name).write_text(source.replace(*patch.split("=>", 1))
                                 if "=>" in patch else source + patch,
                                 encoding="utf-8")
    return load_file(tmp_path / name, LIBRARY)


def test_library_variants_load():
    react = load_file(LIBRARY / "dds-adjacent-react.yaml", LIBRARY)
    silent = load_file(LIBRARY / "dds-adjacent-silent.yaml", LIBRARY)

    assert react.crew_request is not None and silent.crew_request is not None
    assert any(entry.service == "Мособлгаз" for entry in react.adjacent_timeline)
    assert not any(entry.service == "Мособлгаз" for entry in silent.adjacent_timeline)
    assert "Мособлгаз" in react.notify_services()


def test_timeline_service_outside_notify_fails_load(tmp_path):
    with pytest.raises(ScenarioError, match="не входит в список оповещения"):
        load_variant(tmp_path, "service: Мособлгаз=>service: Служба спасения кошек")


def test_crew_request_service_outside_notify_fails_load(tmp_path):
    with pytest.raises(ScenarioError, match="crew_request"):
        load_variant(tmp_path, "  service: Мособлгаз\n  text:=>  service: Чужая служба\n  text:")


def test_timeline_breaking_status_machine_fails_load(tmp_path):
    with pytest.raises(ScenarioError, match="недопустим статус"):
        load_variant(tmp_path, "status: accepted\n    comment: \"Основание: карточка по пожару в"
                               "=>status: completed\n    comment: \"Основание: карточка по пожару в")


def test_timeline_blank_comment_fails_load(tmp_path):
    with pytest.raises(ScenarioError):
        load_variant(
            tmp_path,
            'comment: "Основание: доклад аварийной бригады. Сведения: бригада выехала на перекрытие ввода."'
            '=>comment: " "',
        )


# ── доставка таймлайна ──


def test_timeline_waits_for_card_acceptance():
    state = lesson("dds-adjacent-react")

    assert not deliver_adjacent_due(state, now_utc() + timedelta(seconds=600))
    assert state.desk.active.status_log == []


def test_timeline_marks_adjacent_services_with_simulation_author():
    state = accepted("dds-adjacent-react")
    advance(state, 60)
    card = state.desk.active

    marks = [entry for entry in card.status_log if entry.author == ADJACENT_AUTHOR]
    assert [(entry.service, entry.status) for entry in marks] == [
        ("Мособлгаз", ServiceStatus.ACCEPTED),
        ("МВД", ServiceStatus.ACCEPTED),
        ("Мособлгаз", ServiceStatus.RESPONDING),
    ]
    assert all(entry.comment.strip() for entry in marks)


def test_pause_shifts_timeline():
    state = accepted("dds-adjacent-react")
    now = now_utc()
    state.desk.active.adjacent_started_at = now - timedelta(seconds=30)
    state.pause_intervals.append((now - timedelta(seconds=25), now - timedelta(seconds=5)))

    deliver_adjacent_due(state, now)

    assert state.desk.active.adjacent_next_index == 0, "активных 10 с меньше 20 с таймлайна"


def test_snapshot_shows_adjacent_status_and_keeps_transitions_managed():
    state = accepted("dds-adjacent-react")
    advance(state, 60)

    snapshot = state.station_snapshot()

    assert snapshot.statuses["Мособлгаз"] is ServiceStatus.RESPONDING
    assert "Мособлгаз" not in snapshot.available
    assert set(snapshot.available) == set(snapshot.services)


# ── вводная ──


def test_crew_request_arrives_as_incoming_call():
    state = accepted("dds-adjacent-silent")
    advance(state, 50)
    card = state.desk.active

    call = card.phone_pending
    assert call is not None and call.incoming
    assert call.adjacent_service == "Мособлгаз"
    assert card.crew_request_at is not None
    assert card.phone_lines[-1].speaker == "crew"
    assert "перекрыть ввод" in card.phone_lines[-1].text


def test_classify_reply_understands_reassure_defer_and_negation():
    assert classify_reply(REASSURE) is True
    assert classify_reply(DEFER) is False
    assert classify_reply("Мособлгаз пока не реагирует, разберусь сам") is False
    assert classify_reply("Так точно, понял") is None
    # Утверждение реакции сильнее обещания уточнить, а «будут» само по себе
    # не успокаивание.
    assert classify_reply("Служба реагирует, бригада выехала, сообщу о прибытии") is True
    assert classify_reply("Сведения будут позже, оставайтесь на связи") is None


def test_phone_reply_closes_incoming_call_and_records_claim():
    state = accepted("dds-adjacent-silent")
    advance(state, 50)
    card = state.desk.active

    outcome = state.desk.apply(PhoneReply(text=REASSURE), state)

    assert outcome.error is None and outcome.changed
    assert card.phone_pending is None
    assert card.adjacent_reply_claimed is True
    assert ("adjacent.reply", "reassure") in [
        (action, detail) for action, _at, detail in card.dds_log]


def test_incoming_call_cannot_be_dropped_or_abandoned():
    """Сброс и закрытие карточки закрыли бы вводную без ответа навсегда."""
    from app.domain.events import CardNext, PhoneHangup

    state = accepted("dds-adjacent-silent")
    advance(state, 50)
    card = state.desk.active

    hangup = state.desk.apply(PhoneHangup(), state)
    closing = state.desk.apply(CardNext(card_id=card.card_id), state)

    assert hangup.error is not None and "Ответьте бригаде" in hangup.error
    assert closing.error == "Сначала ответьте бригаде на входящий вызов"
    assert card.phone_pending is not None


def test_crew_request_waits_for_card_to_be_open():
    """Входящий вызов фоновой карточки курсант не увидел бы — вводная ждёт."""
    scenario = load_file(LIBRARY / "dds-adjacent-silent.yaml", LIBRARY)
    other = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
    )
    prepare_queue(state, [scenario, other])
    first = state.desk.active
    assert state.desk.apply(CardAck(comment=BASIS), state).changed
    second = next(card for card in state.desk.ordered() if card is not first)
    state.desk.open(second.card_id)

    first.adjacent_started_at -= timedelta(seconds=60)
    deliver_adjacent_due(state)
    assert first.crew_request_at is None and first.phone_pending is None

    state.desk.open(first.card_id)
    deliver_adjacent_due(state)
    assert first.crew_request_at is not None and first.phone_pending.incoming


def test_repeated_adjacent_dial_is_refused():
    state = accepted("dds-adjacent-silent")
    advance(state, 50)
    state.desk.apply(PhoneReply(text=DEFER), state)
    assert state.desk.apply(AdjacentDial(service="Мособлгаз", text=REQUEST), state).error is None
    lines = len(state.desk.active.phone_lines)

    outcome = state.desk.apply(AdjacentDial(service="Мособлгаз", text=REQUEST), state)

    assert outcome.error == "Разговор с этой службой уже состоялся; сведения — в журнале"
    assert len(state.desk.active.phone_lines) == lines


def test_phone_reply_without_incoming_call_is_refused():
    state = accepted("dds-adjacent-silent")

    outcome = state.desk.apply(PhoneReply(text=REASSURE), state)

    assert outcome.error == "Нет входящего вызова бригады"


def test_adjacent_dial_requires_answering_crew_first():
    state = accepted("dds-adjacent-silent")
    advance(state, 50)

    outcome = state.desk.apply(AdjacentDial(service="Мособлгаз", text=REQUEST), state)

    assert outcome.error == "Сначала ответьте бригаде"


def test_adjacent_dial_scripts_answer_and_marks_status():
    state = accepted("dds-adjacent-silent")
    advance(state, 50)
    state.desk.apply(PhoneReply(text=DEFER), state)
    card = state.desk.active

    outcome = state.desk.apply(AdjacentDial(service="Мособлгаз", text=REQUEST), state)

    assert outcome.error is None and len(outcome.events) == 2
    mark = card.status_log[-1]
    assert (mark.service, mark.status, mark.author) == (
        "Мособлгаз", ServiceStatus.ACCEPTED, ADJACENT_CALL_AUTHOR)
    assert card.adjacent_call_at is not None


def test_adjacent_dial_to_own_or_unknown_service_is_refused():
    state = accepted("dds-adjacent-silent")
    advance(state, 50)
    state.desk.apply(PhoneReply(text=DEFER), state)

    for service in ("Служба 101", "Чужая служба"):
        outcome = state.desk.apply(AdjacentDial(service=service, text=REQUEST), state)
        assert outcome.error == "Звонить можно смежной службе из списка оповещения карточки"


# ── метрики ──


def metrics_by_key(state):
    card = state.desk.active
    metrics, findings = adjacent_metrics(
        card, state.criteria.dds_adjacent_limit_seconds * 1000, state.pause_intervals)
    return {metric.key: metric for metric in metrics}, findings


def test_no_request_no_metrics():
    state = accepted("dds-adjacent-react")

    metrics, findings = metrics_by_key(state)

    assert metrics == {} and findings == []


def test_reassure_after_reaction_passes_check():
    state = accepted("dds-adjacent-react")
    advance(state, 90)
    state.desk.apply(PhoneReply(text=REASSURE), state)

    metrics, findings = metrics_by_key(state)

    assert metrics["dds_adjacent_check"].passed
    assert "dds_adjacent_call" not in metrics, "перестраховочная метрика не выставляется"
    assert findings == []


def test_reassure_without_reaction_fails_check():
    state = accepted("dds-adjacent-silent")
    advance(state, 50)
    state.desk.apply(PhoneReply(text=REASSURE), state)

    metrics, findings = metrics_by_key(state)

    assert not metrics["dds_adjacent_check"].passed
    assert any("без сверки" in finding.summary for finding in findings)


def test_ignored_request_fails_metric_of_each_outcome():
    react = accepted("dds-adjacent-react")
    advance(react, 90)
    silent = accepted("dds-adjacent-silent")
    advance(silent, 50)

    react_metrics, _ = metrics_by_key(react)
    silent_metrics, _ = metrics_by_key(silent)

    assert not react_metrics["dds_adjacent_check"].passed
    assert not silent_metrics["dds_adjacent_call"].passed


def test_silent_service_called_in_time_passes():
    state = accepted("dds-adjacent-silent")
    advance(state, 50)
    state.desk.apply(PhoneReply(text=DEFER), state)
    state.desk.apply(AdjacentDial(service="Мособлгаз", text=REQUEST), state)

    metrics, findings = metrics_by_key(state)

    assert metrics["dds_adjacent_call"].passed
    assert findings == []


def test_silent_service_never_called_fails_call_metric():
    state = accepted("dds-adjacent-silent")
    advance(state, 50)
    state.desk.apply(PhoneReply(text=DEFER), state)

    metrics, findings = metrics_by_key(state)

    assert not metrics["dds_adjacent_call"].passed
    assert any("без звонка" in finding.summary for finding in findings)


def test_call_after_limit_fails_call_metric():
    state = accepted("dds-adjacent-silent")
    advance(state, 50)
    state.desk.apply(PhoneReply(text=DEFER), state)
    state.desk.apply(AdjacentDial(service="Мособлгаз", text=REQUEST), state)
    card = state.desk.active
    card.crew_request_at -= timedelta(seconds=state.criteria.dds_adjacent_limit_seconds + 30)
    card.adjacent_reply_at -= timedelta(seconds=state.criteria.dds_adjacent_limit_seconds + 30)

    metrics, _ = metrics_by_key(state)

    assert not metrics["dds_adjacent_call"].passed


def test_reassure_after_own_call_counts_as_reaction():
    """Курсант позвонил молчащей службе, статус появился — успокоить бригаду теперь можно."""
    state = accepted("dds-adjacent-silent")
    advance(state, 50)
    state.desk.apply(PhoneReply(text=DEFER), state)
    state.desk.apply(AdjacentDial(service="Мособлгаз", text=REQUEST), state)
    card = state.desk.active
    card.adjacent_reply_claimed = True
    card.adjacent_reply_at = now_utc()

    metrics, _ = metrics_by_key(state)

    assert metrics["dds_adjacent_check"].passed
