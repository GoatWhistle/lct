"""Готовая карточка → учебный звонок бригаде → числовая оценка ДДС."""

import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.domain.timers import TimerCode
from app.session.hub import hub


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        test_client.post("/api/auth/dev-token")
        hub.journal = None
        yield test_client


def wait_for(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("состояние не обновилось")


def read_until(socket, wanted):
    for _ in range(20):
        event = socket.receive_json()
        if event["type"] == wanted:
            return event
    raise AssertionError(f"событие {wanted} не пришло")


def start(client, exercise="dds"):
    session_id = uuid4()
    context = client.websocket_connect(f"/ws/control/{session_id}")
    control = context.__enter__()
    control.send_json({"type": "scenario.start", "scenario_id": "fire-apartment-l2",
                       "trainee": "Иванов", "mode": "training", "exercise": exercise})
    wait_for(lambda: hub.get(session_id))
    return session_id, context


def test_dds_starts_with_prepared_card_without_call(client):
    session_id, control = start(client)
    try:
        state = hub.get(session_id)
        assert state.exercise.value == "dds"
        assert state.dispatched_card is not None
        assert state.kio.address == "улица Ленина, 14"
        assert state.kio.notify
        assert state.timers.measured_ms(TimerCode.ANSWER) is None
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            card = read_until(station, "card.received")
            assert card["from_operator"] == "учебный сценарий"
            assert card["card"]["address"] == "улица Ленина, 14"
            snapshot = read_until(station, "station.state")["snapshot"]
            assert snapshot["crew_options"]
    finally:
        control.__exit__(None, None, None)

def test_dds_call_reports_gate_status_and_affect_numeric_score(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            service = snapshot["services"][0]
            crew = next(option for option in snapshot["crew_options"] if option.startswith(service + " — "))
            station.send_json({"type": "card.status", "service": service, "status": "accepted"})
            read_until(station, "station.state")
            station.send_json({"type": "card.status", "service": service, "status": "responding"})
            assert "доклада" in read_until(station, "error")["message"]
            station.send_json({"type": "crew.select", "crew": crew})
            selected = read_until(station, "station.state")["snapshot"]
            assert selected["crew_selected"] == crew
            station.send_json({"type": "phone.dial"})
            report = read_until(station, "phone.report")
            assert report["phase"] == "dispatched" and report["crew"] == crew
            assert read_until(station, "station.state")["snapshot"]["phone_reports"]
            station.send_json({"type": "card.status", "service": service, "status": "responding"})
            read_until(station, "station.state")
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")
        score = wait_for(lambda: hub.get(session_id).score)
        keys = {metric["key"] for metric in score["metrics"]}
        assert "dds_primary" in keys and "dds_contact" in keys
        assert "answer_time" not in keys and "interview_time" not in keys
        assert 0 < score["score_auto"] < 100
        assert all(not finding["code"].startswith("E") for finding in score["findings"])
    finally:
        control.__exit__(None, None, None)


def test_default_exercise_remains_call(client):
    session_id = uuid4()
    with client.websocket_connect(f"/ws/control/{session_id}") as control:
        control.send_json({"type": "scenario.start", "scenario_id": "fire-apartment-l2",
                           "trainee": "Иванов", "mode": "training"})
        state = wait_for(lambda: hub.get(session_id))
        assert state.exercise.value == "call"
        assert state.dispatched_card is None


def test_complete_dds_workflow_scores_without_call_penalties(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            for service in snapshot["services"]:
                crew = next(option for option in snapshot["crew_options"]
                            if option.startswith(service + " — "))
                station.send_json({"type": "card.status", "service": service, "status": "accepted"})
                read_until(station, "station.state")
                station.send_json({"type": "crew.select", "crew": crew})
                read_until(station, "station.state")
                for expected_phase, status in (
                    ("dispatched", "responding"), ("arrived", "arrived"),
                    ("working", "working"), ("completed", "completed"),
                ):
                    station.send_json({"type": "phone.dial"})
                    assert read_until(station, "phone.report")["phase"] == expected_phase
                    read_until(station, "station.state")
                    station.send_json({"type": "card.status", "service": service, "status": status})
                    snapshot = read_until(station, "station.state")["snapshot"]
            assert snapshot["card"] == "completed"
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")
        score = wait_for(lambda: hub.get(session_id).score)
        assert score["score_auto"] == 100.0
        assert not score["findings"]
        assert all(metric["key"].startswith("dds_") for metric in score["metrics"])
    finally:
        control.__exit__(None, None, None)


def test_sequential_dds_cards_keep_separate_state_and_scores(client):
    session_id = uuid4()
    control_ctx = client.websocket_connect(f"/ws/control/{session_id}")
    control = control_ctx.__enter__()
    control.send_json({
        "type": "scenario.start", "scenario_id": "fire-apartment-l2",
        "scenario_ids": ["fire-apartment-l2", "t20-2-stroke"],
        "trainee": "Иванов", "mode": "training", "exercise": "dds",
    })
    wait_for(lambda: hub.get(session_id))
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            first = read_until(station, "card.received")
            first_card_id = first["card"]["card_id"]
            assert (first["card_index"], first["card_total"]) == (1, 2)
            snapshot = read_until(station, "station.state")["snapshot"]
            service = snapshot["services"][0]
            station.send_json({"type": "card.status", "service": service, "status": "accepted"})
            read_until(station, "station.state")
            station.send_json({"type": "card.reply", "card_id": first_card_id,
                               "text": "Сообщение принято, дежурная бригада направлена на место."})
            assert read_until(station, "station.state")["snapshot"]["reply_text"].startswith("Сообщение")
            station.send_json({"type": "card.next", "card_id": first_card_id})
            second = read_until(station, "card.received")
            second_card_id = second["card"]["card_id"]
            assert second_card_id != first_card_id
            assert second["card"]["incident_type"] == "medical"
            assert (second["card_index"], second["card_total"]) == (2, 2)
            snapshot = read_until(station, "station.state")["snapshot"]
            assert snapshot["reply_text"] == ""
            assert snapshot["statuses"].get(service) == "added" or service not in snapshot["statuses"]
            assert len(snapshot["completed_cards"]) == 1
            assert snapshot["completed_cards"][0]["card_id"] == first_card_id
            station.send_json({"type": "card.reply", "card_id": first_card_id,
                               "text": "Запоздалый ответ к прошлой карточке"})
            assert "не к текущей" in read_until(station, "error")["message"]
            assert hub.get(session_id).reply_text == ""
            station.send_json({"type": "card.next", "card_id": first_card_id})
            assert "ID" in read_until(station, "error")["message"]
            assert hub.get(session_id).dds_card_index == 1

        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            assert read_until(station, "card.received")["card"]["card_id"] == second_card_id
            assert read_until(station, "station.state")["snapshot"]["card_index"] == 2
            station.send_json({"type": "card.reply", "card_id": second_card_id,
                               "text": "Сообщение принято, бригада направлена на место происшествия."})
            read_until(station, "station.state")
            station.send_json({"type": "card.next", "card_id": second_card_id})
            read_until(station, "score.ready")

        state = hub.get(session_id)
        assert state.ended and len(state.dds_completed) == 2
        assert len(state.score["card_results"]) == 2
        assert state.score["card_results"][0]["scenario_id"] == "fire-apartment-l2"
        assert state.score["card_results"][1]["scenario_id"] == "t20-2-stroke"
        assert {item["key"] for item in state.score["metrics"]} >= {"dds_reply", "dds_primary"}
        assert all(item["code"].startswith("D") for item in state.score["findings"])
        report = client.get(f"/api/sessions/{session_id}/report").json()
        assert report["scenario_id"] == "fire-apartment-l2"
        assert len(report["card_results"]) == 2
        assert report["missed_checklist"] == [] and report["reference_questions"] == []
        first_actions = report["card_results"][0]["actions"]
        assert {item["type"] for item in first_actions} >= {"card.status", "card.reply"}
        assert report["card_results"][0]["duration_ms"] >= 0
    finally:
        control_ctx.__exit__(None, None, None)


def test_instructor_can_end_multi_card_early_without_grading_future_cards(client):
    session_id = uuid4()
    control_ctx = client.websocket_connect(f"/ws/control/{session_id}")
    control = control_ctx.__enter__()
    control.send_json({
        "type": "scenario.start", "scenario_id": "fire-apartment-l2",
        "scenario_ids": ["fire-apartment-l2", "t20-2-stroke"],
        "trainee": "Иванов", "mode": "training", "exercise": "dds",
    })
    wait_for(lambda: hub.get(session_id))
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            read_until(station, "station.state")
            control.send_json({"type": "session.stop"})
            read_until(station, "score.ready")
        state = hub.get(session_id)
        assert state.ended
        assert len(state.score["card_results"]) == 1
        assert state.score["card_results"][0]["scenario_id"] == "fire-apartment-l2"
    finally:
        control_ctx.__exit__(None, None, None)


def test_each_dds_card_uses_its_own_scenario_weights():
    from pathlib import Path

    from app.domain.events import Exercise, SessionMode
    from app.scenarios.loader import load_file
    from app.session.dds import prepare_card
    from app.session.finish import score_current_dds
    from app.session.state import SessionState

    root = Path(__file__).resolve().parents[2] / "scenarios"
    base = load_file(root / "fire-apartment-l2.yaml", root)
    first = base.model_copy(deep=True)
    second = base.model_copy(deep=True)
    first.score_weights = {"dds_reply": 7.0}
    second.score_weights = {"dds_reply": 2.0}
    state = SessionState(
        session_id=uuid4(), scenario_id=base.id, scenario_title=base.title,
        level=base.level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
        dds_scenarios=[first, second],
    )
    prepare_card(state, first)
    first_record = score_current_dds(state)
    state.dds_card_index = 1
    prepare_card(state, second)
    second_record = score_current_dds(state)
    assert next(item.weight for item in first_record.metrics if item.key == "dds_reply") == 7.0
    assert next(item.weight for item in second_record.metrics if item.key == "dds_reply") == 2.0
