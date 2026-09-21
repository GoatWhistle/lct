"""Текстовая вводная 112: карточка без голоса, опроса и ДДС-оценки."""

import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.session.hub import hub


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        test_client.post("/api/auth/dev-token")
        hub.journal = None
        yield test_client


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("не дождались обновления состояния")


def read_until(socket, wanted):
    for _ in range(20):
        event = socket.receive_json()
        if event["type"] == wanted:
            return event
    raise AssertionError(f"событие {wanted} не пришло")


def start(client):
    session_id = uuid4()
    context = client.websocket_connect(f"/ws/control/{session_id}")
    control = context.__enter__()
    control.send_json({"type": "scenario.start", "scenario_id": "fire-apartment-l2",
                       "trainee": "Иванов", "mode": "training", "exercise": "card"})
    wait_for(lambda: hub.get(session_id))
    return session_id, context


def test_card_briefing_is_text_only_and_replayed_on_late_join(client):
    session_id, control = start(client)
    try:
        state = hub.get(session_id)
        assert state.exercise.value == "card"
        assert state.dispatched_card is None
        assert state.slots is None and state.voice is None
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            briefing = read_until(trainee, "card.briefing")
            assert briefing["scenario_id"] == "fire-apartment-l2"
            assert "Помогите" in briefing["text"]
            assert "горит балкон" in briefing["text"]
            assert "ground_truth" not in briefing
            assert briefing["card"]["address"] is None
            assert "address" in briefing["required_fields"]
            trainee.send_bytes(b"\0" * 640)
            assert read_until(trainee, "error")["code"] == "unsupported_event"
        with client.websocket_connect(f"/ws/call/{session_id}") as late:
            assert read_until(late, "card.briefing")["text"] == briefing["text"]
    finally:
        control.__exit__(None, None, None)


def test_correct_card_scores_100_without_call_or_dds_metrics(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
                briefing = read_until(trainee, "card.briefing")
                assert "dds" not in briefing["required_fields"]
                trainee.send_json({"type": "kio.patch", "fields": {
                    "address": "улица Ленина, 14", "floor": "5", "incident_type": "fire",
                    "victims_count": 2, "description": "горит балкон",
                    "signs": ["жилой дом", "балкон", "открытое пламя"],
                }})
                wait_for(lambda: hub.get(session_id).kio.incident_code)
                trainee.send_json({"type": "card.submit"})
                assert read_until(trainee, "call.ended")["reason"] == "complete"
                assert read_until(trainee, "score.ready")["session_id"] == str(session_id)
            received = read_until(station, "card.received")
            assert received["card"]["address"] == "улица Ленина, 14"
            read_until(station, "station.state")
        state = hub.get(session_id)
        assert state.dispatched_card is not None
        assert state.score["score_auto"] == 100.0
        assert not state.score["findings"]
        assert {item["key"] for item in state.score["metrics"]} == {
            "incident_signs", "address", "victims_count", "required_fields"
        }
        with client.websocket_connect(f"/ws/call/{session_id}") as late:
            assert read_until(late, "card.briefing")["card"]["address"] == "улица Ленина, 14"
            assert read_until(late, "call.ended")["reason"] == "complete"
            assert read_until(late, "score.ready")["session_id"] == str(session_id)
    finally:
        control.__exit__(None, None, None)


def test_incomplete_card_has_only_card_findings(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            read_until(trainee, "card.briefing")
            trainee.send_json({"type": "card.submit"})
            read_until(trainee, "score.ready")
            trainee.send_json({"type": "kio.patch", "fields": {"address": "после сдачи"}})
            assert read_until(trainee, "error")["code"] == "unsupported_event"
        state = hub.get(session_id)
        assert state.kio.address is None
        assert state.score["score_auto"] < 100
        assert {item["code"] for item in state.score["findings"]} <= {"E2", "E5"}
        assert all(item["key"] not in {"answer_time", "interview_time", "dds_ack"}
                   for item in state.score["metrics"])
        report = client.get(f"/api/sessions/{session_id}/report").json()
        assert report["missed_checklist"] == []
        assert report["reference_questions"] == []
    finally:
        control.__exit__(None, None, None)
