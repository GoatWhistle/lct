"""Готовая карточка → учебный звонок бригаде → числовая оценка ДДС."""

import time
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.timers import TimerCode
from app.main import app
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


def start(client, exercise="dds", criteria=None, dds_service=None, scenario_id="fire-apartment-l2"):
    session_id = uuid4()
    context = client.websocket_connect(f"/ws/control/{session_id}")
    control = context.__enter__()
    payload = {"type": "scenario.start", "scenario_id": scenario_id,
               "trainee": "Иванов", "mode": "training", "exercise": exercise}
    if criteria is not None:
        payload["criteria"] = criteria
    if dds_service is not None:
        payload["dds_service"] = dds_service
    control.send_json(payload)
    wait_for(lambda: hub.get(session_id))
    return session_id, context


def complete_phone_call(station, state, expected_phase):
    station.send_json({"type": "phone.dial"})
    greeting = read_until(station, "phone.line")
    assert greeting["speaker"] == "crew"
    read_until(station, "station.state")
    if expected_phase == "dispatched":
        station.send_json({"type": "phone.brief", "address": state.dispatched_card.address,
                           "incident": state.scenario_title,
                           "request": "Прошу подтвердить выезд и сообщить о прибытии"})
    else:
        station.send_json({"type": "phone.check", "text": "Сообщите текущую обстановку по карточке"})
    assert read_until(station, "phone.line")["speaker"] == "dispatcher"
    assert read_until(station, "phone.line")["speaker"] == "crew"
    report = read_until(station, "phone.report")
    assert report["phase"] == expected_phase
    return report


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
            assert len(snapshot["services"]) == 1
            assert snapshot["managed_service"] == snapshot["services"][0]
            assert set(snapshot["services"]) < set(snapshot["recipient_services"])
    finally:
        control.__exit__(None, None, None)


def test_ticket_dds_card_uses_source_caller_identity_and_phone(client):
    session_id, control = start(client, scenario_id="t01-1-fire-container")
    try:
        state = hub.get(session_id)
        assert state.kio.caller_name == "Сидоров Иван Сергеевич"
        assert state.kio.caller_number == "916-126-34-71"
        assert state.kio.caller_contact == "916-126-34-71"
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            card = read_until(station, "card.received")["card"]
            assert card["caller_name"] == "Сидоров Иван Сергеевич"
            assert card["caller_number"] == "916-126-34-71"
            assert card["caller_contact"] == "916-126-34-71"
    finally:
        control.__exit__(None, None, None)


def test_ticket_with_multiple_witnesses_does_not_guess_caller_name(client):
    session_id, control = start(client, scenario_id="t20-1-three-fighting")
    try:
        state = hub.get(session_id)
        assert state.kio.caller_name is None
        assert state.kio.caller_number == "916-123-98-78"
        assert state.kio.caller_contact == "916-123-98-78"
    finally:
        control.__exit__(None, None, None)


def test_dds_can_change_only_its_own_service_status(client):
    session_id, control = start(client, dds_service="МВД")
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            assert snapshot["services"] == ["МВД"]
            assert "Служба 101" in snapshot["recipient_services"]
            station.send_json({
                "type": "card.status", "service": "Служба 101", "status": "accepted",
            })
            assert "только своей ДДС" in read_until(station, "error")["message"]
            station.send_json({
                "type": "card.status", "service": "МВД", "status": "accepted",
            })
            assert read_until(station, "station.state")["snapshot"]["statuses"]["МВД"] == "accepted"
    finally:
        control.__exit__(None, None, None)


def test_instructor_criteria_change_timer_and_success_threshold(client):
    session_id, control = start(client, criteria={
        "decision_time_limit_seconds": 45,
        "allowed_errors": 50,
        "require_correct_grammar": False,
        "score_weights": {"dds_ack": 3.5},
    })
    try:
        state = hub.get(session_id)
        assert state.criteria.decision_time_limit_seconds == 45
        assert state.timers.limits[TimerCode.DDS_ACK] == 45_000
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            card = read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            service = snapshot["services"][0]
            station.send_json({"type": "card.status", "service": service, "status": "accepted"})
            read_until(station, "station.state")
            station.send_json({"type": "card.reply", "card_id": card["card"]["card_id"],
                               "text": "Сообщение приняты, бригада направлено."})
            read_until(station, "station.state")
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")

        score = wait_for(lambda: hub.get(session_id).score)
        ack = next(item for item in score["metrics"] if item["key"] == "dds_ack")
        assert ack["norm"] == "≤ 45 с"
        assert ack["weight"] == 3.5
        assert not any(item["key"] in {"dds_reply", "dds_grammar"} for item in score["metrics"])
        report = client.get(f"/api/sessions/{session_id}/report").json()
        assert report["criteria"] == {
            "decision_time_limit_seconds": 45,
            "allowed_errors": 50,
            "require_correct_grammar": False,
            "score_weights": {"dds_ack": 3.5},
        }
        assert report["failed_metrics"] > 0
        assert report["passed"] is True
    finally:
        control.__exit__(None, None, None)

def test_dds_statuses_do_not_require_phone_reports(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            service = snapshot["services"][0]
            station.send_json({"type": "card.status", "service": service, "status": "accepted"})
            read_until(station, "station.state")
            station.send_json({"type": "card.status", "service": service, "status": "responding"})
            assert read_until(station, "station.state")["snapshot"]["statuses"][service] == "responding"
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")
        score = wait_for(lambda: hub.get(session_id).score)
        keys = {metric["key"] for metric in score["metrics"]}
        assert "dds_primary" in keys and "dds_progress" in keys
        assert "dds_contact" not in keys and "dds_crew" not in keys
        assert "answer_time" not in keys and "interview_time" not in keys
        assert 0 < score["score_auto"] < 100
        assert all(not finding["code"].startswith("E") for finding in score["findings"])
    finally:
        control.__exit__(None, None, None)


def test_phone_dial_requires_brief_and_rejects_wrong_card_facts(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            service = snapshot["services"][0]
            crew = next(option for option in snapshot["crew_options"] if option.startswith(service + " — "))
            station.send_json({"type": "card.status", "service": service, "status": "accepted"})
            read_until(station, "station.state")
            station.send_json({"type": "crew.select", "crew": crew})
            read_until(station, "station.state")
            station.send_json({"type": "phone.dial"})
            assert read_until(station, "phone.line")["speaker"] == "crew"
            pending = read_until(station, "station.state")["snapshot"]
            assert pending["phone_pending"]["phase"] == "dispatched"
            assert not pending["phone_reports"]
            station.send_json({"type": "card.status", "service": service, "status": "responding"})
            assert read_until(station, "station.state")["snapshot"]["statuses"][service] == "responding"
            station.send_json({"type": "phone.brief", "address": "другая улица, дом 99",
                               "incident": "Пожар в квартире",
                               "request": "Прошу направить бригаду"})
            assert "адрес" in read_until(station, "error")["message"]
            station.send_json({"type": "phone.brief", "address": "улица Ленина, 14",
                               "incident": "Ничего не произошло",
                               "request": "Прошу направить бригаду"})
            assert "характер" in read_until(station, "error")["message"]
            station.send_json({"type": "phone.hangup"})
            assert read_until(station, "station.state")["snapshot"]["phone_pending"] is None
            assert not hub.get(session_id).phone_reports
            complete_phone_call(station, hub.get(session_id), "dispatched")
            read_until(station, "station.state")
            assert len(hub.get(session_id).phone_reports) == 1
            station.send_json({"type": "phone.dial"})
            read_until(station, "phone.line")
            read_until(station, "station.state")
            station.send_json({"type": "phone.check", "text": "Здравствуйте, хорошая погода"})
            assert "обстановку" in read_until(station, "error")["message"]
            assert len(hub.get(session_id).phone_reports) == 1
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


def test_complete_dds_workflow_scores_100_without_any_call(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            for service in snapshot["services"]:
                station.send_json({"type": "card.status", "service": service, "status": "accepted"})
                read_until(station, "station.state")
                for status in ("responding", "arrived", "working", "completed"):
                    station.send_json({"type": "card.status", "service": service, "status": status})
                    snapshot = read_until(station, "station.state")["snapshot"]
            assert snapshot["card"] == "completed"
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")
        score = wait_for(lambda: hub.get(session_id).score)
        assert score["score_auto"] == 100.0
        assert not score["findings"]
        assert all(metric["key"].startswith("dds_") for metric in score["metrics"])
        assert not any(metric["key"] in {"dds_contact", "dds_crew", "dds_reply", "dds_grammar"}
                       for metric in score["metrics"])
    finally:
        control.__exit__(None, None, None)


def test_concurrent_dds_cards_keep_independent_state_and_timers(client):
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
            assert len(snapshot["queue_cards"]) == 2
            first_row, second_row = snapshot["queue_cards"]
            second_card_id = second_row["card_id"]
            assert first_row["active"] is True and second_row["active"] is False
            assert abs(
                datetime.fromisoformat(first_row["received_at"]).timestamp()
                - datetime.fromisoformat(second_row["received_at"]).timestamp()
            ) < 1
            first_service = snapshot["services"][0]
            station.send_json({"type": "card.status", "service": first_service,
                               "status": "accepted"})
            snapshot = read_until(station, "station.state")["snapshot"]
            first_elapsed = next(item for item in snapshot["queue_cards"]
                                 if item["card_id"] == first_card_id)["elapsed_ms"]

            time.sleep(0.03)
            # Card switching must publish its own fresh station snapshot; do
            # not let the one-second ticker mask a missing update.
            hub.stop_ticker(session_id)
            station.send_json({"type": "card.open", "card_id": second_card_id})
            second = read_until(station, "card.received")
            assert second["card"]["card_id"] == second_card_id
            assert second["card"]["incident_type"] == "medical"
            assert (second["card_index"], second["card_total"]) == (2, 2)
            snapshot = read_until(station, "station.state")["snapshot"]
            second_queue_row = next(item for item in snapshot["queue_cards"]
                                    if item["card_id"] == second_card_id)
            assert second_queue_row["active"] is True
            assert second_queue_row["elapsed_ms"] >= first_elapsed
            first_queue_row = next(item for item in snapshot["queue_cards"]
                                   if item["card_id"] == first_card_id)
            assert first_queue_row["service_status"] == "accepted"
            assert first_queue_row["timer_stopped"] is True

            second_service = snapshot["services"][0]
            station.send_json({"type": "card.status", "service": second_service,
                               "status": "accepted"})
            read_until(station, "station.state")
            station.send_json({"type": "card.open", "card_id": first_card_id})
            assert read_until(station, "card.received")["card"]["card_id"] == first_card_id
            restored = read_until(station, "station.state")["snapshot"]
            assert restored["statuses"][first_service] == "accepted"
            station.send_json({"type": "card.next", "card_id": first_card_id})
            assert read_until(station, "card.received")["card"]["card_id"] == second_card_id
            after_close = read_until(station, "station.state")["snapshot"]
            assert len(after_close["queue_cards"]) == 1
            assert after_close["statuses"][second_service] == "accepted"
            station.send_json({"type": "card.next", "card_id": second_card_id})
            read_until(station, "score.ready")

        state = hub.get(session_id)
        assert state.ended and len(state.dds_completed) == 2
        assert len(state.score["card_results"]) == 2
        assert state.score["card_results"][0]["scenario_id"] == "fire-apartment-l2"
        assert state.score["card_results"][1]["scenario_id"] == "t20-2-stroke"
        assert "dds_primary" in {item["key"] for item in state.score["metrics"]}
        assert not any(item["key"] in {"dds_reply", "dds_grammar"}
                       for item in state.score["metrics"])
        assert all(item["code"].startswith("D") for item in state.score["findings"])
        report = client.get(f"/api/sessions/{session_id}/report").json()
        assert report["scenario_id"] == "fire-apartment-l2"
        assert len(report["card_results"]) == 2
        assert report["missed_checklist"] == [] and report["reference_questions"] == []
        first_actions = report["card_results"][0]["actions"]
        assert {item["type"] for item in first_actions} >= {"card.status"}
        assert report["card_results"][0]["duration_ms"] >= 0
    finally:
        control_ctx.__exit__(None, None, None)


def test_instructor_end_grades_all_concurrently_issued_cards(client):
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
        assert len(state.score["card_results"]) == 2
        assert [item["scenario_id"] for item in state.score["card_results"]] == [
            "fire-apartment-l2", "t20-2-stroke",
        ]
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
    first.score_weights = {"dds_primary": 7.0}
    second.score_weights = {"dds_primary": 2.0}
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
    assert next(item.weight for item in first_record.metrics if item.key == "dds_primary") == 7.0
    assert next(item.weight for item in second_record.metrics if item.key == "dds_primary") == 2.0
