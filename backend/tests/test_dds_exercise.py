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
