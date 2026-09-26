"""Текстовое упражнение 112: переписка с заявителем без голоса."""

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.http import sessions as sessions_http
from app.main import app
from app.api.ws.call import _text_turn
from app.scenarios import store
from app.scoring.grammar import basic_check
from app.session.hub import hub


@pytest.fixture
def client(monkeypatch):
    async def audit_in_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sessions_http, "audit_required", audit_in_memory)
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


async def rules_only_grammar(text):
    return basic_check(text)


def start(client, *, handoff_to_dds=False, criteria=None, scenario_ids=None):
    session_id = uuid4()
    context = client.websocket_connect(f"/ws/control/{session_id}")
    control = context.__enter__()
    payload = {"type": "scenario.start", "scenario_id": "fire-apartment-l2",
               "trainee": "Иванов", "mode": "training", "exercise": "card",
               "handoff_to_dds": handoff_to_dds}
    if scenario_ids is not None:
        payload["scenario_ids"] = scenario_ids
    if criteria is not None:
        payload["criteria"] = criteria
    control.send_json(payload)
    wait_for(lambda: hub.get(session_id))
    return session_id, context


def test_card_briefing_is_text_only_and_replayed_on_late_join(client):
    session_id, control = start(client)
    try:
        state = hub.get(session_id)
        assert state.exercise.value == "card"
        assert state.dispatched_card is None
        assert state.voice is None
        assert state.caller is not None and state.persona is not None
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            briefing = read_until(trainee, "card.briefing")
            assert briefing["scenario_id"] == "fire-apartment-l2"
            assert "Помогите" in briefing["text"]
            assert "горит балкон" not in briefing["text"]
            assert "ground_truth" not in briefing
            assert briefing["card"]["address"] is None
            assert "address" in briefing["required_fields"]
            trainee.send_bytes(b"\0" * 640)
            assert read_until(trainee, "error")["code"] == "unsupported_event"
        with client.websocket_connect(f"/ws/call/{session_id}") as late:
            assert read_until(late, "card.briefing")["text"] == briefing["text"]
    finally:
        control.__exit__(None, None, None)


def test_correct_card_scores_100_including_grammar_without_call_or_dds_metrics(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
                briefing = read_until(trainee, "card.briefing")
                assert "dds" not in briefing["required_fields"]
                trainee.send_json({"type": "kio.patch", "fields": {
                    "address": "улица Ленина, 14", "floor": "5", "incident_type": "fire",
                    "victims_count": 2, "description": "Горит балкон.",
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
        grammar_metric = next(item for item in state.score["metrics"]
                              if item["key"] == "description_grammar")
        assert grammar_metric["passed"]
        assert {item["key"] for item in state.score["metrics"]} == {
            "incident_signs", "address", "victims_count", "required_fields",
            "description_grammar", "card_fill_time",
        }
        assert state.score["summary"]["card_fill_ms"] is not None
        fill_metric = next(item for item in state.score["metrics"] if item["key"] == "card_fill_time")
        assert fill_metric["passed"]
        assert "норматива" in fill_metric["fact"]
        with client.websocket_connect(f"/ws/call/{session_id}") as late:
            assert read_until(late, "card.briefing")["card"]["address"] == "улица Ленина, 14"
            assert read_until(late, "call.ended")["reason"] == "complete"
            assert read_until(late, "score.ready")["session_id"] == str(session_id)
    finally:
        control.__exit__(None, None, None)


def test_text_exercise_allows_questions_and_returns_grounded_caller_reply(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            read_until(trainee, "card.briefing")
            trainee.send_json({"type": "text.turn", "text": "Что горит?"})
            accepted = read_until(trainee, "text.turn.accepted")
            assert accepted["text"] == "Что горит?"
            reply = read_until(trainee, "caller.utterance")
            assert "балкон" in reply["text"] or "загорел" in reply["text"]
            assert hub.get(session_id).text_revealed_facts["f_what_burns"] == "горит балкон, дым пошёл в квартиру"
    finally:
        control.__exit__(None, None, None)


def test_natural_request_for_precise_address_reveals_refinement_without_embedder(client):
    scenario = store.get("t01-1-fire-container")
    assert scenario is not None
    address = next(fact for fact in scenario.facts if fact.id == "f_address")
    assert address.refined == scenario.ground_truth.address
    state = SimpleNamespace(
        scenario=scenario,
        slots=None,
        text_revealed_facts={address.id: address.value},
    )

    turn = _text_turn(state, "а точнее можете назвать на ближайшем доме?")

    assert turn.refined == ["f_address"]
    assert turn.matched == ["q_address_check"]
    assert state.text_revealed_facts[address.id] == address.refined


def test_card_fill_overrun_is_reported_as_e3_with_actual_and_norm(client):
    session_id, control = start(client, criteria={"card_fill_time_limit_seconds": 60})
    try:
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            read_until(trainee, "card.briefing")
            state = hub.get(session_id)
            assert state.timers.limits[next(code for code in state.timers.limits
                                            if code.value == "card_fill")] == 60_000
            state.timers.timers[next(code for code in state.timers.timers
                                     if code.value == "card_fill")].started_at = time.monotonic() - 61
            trainee.send_json({"type": "card.submit"})
            read_until(trainee, "call.ended")
            read_until(trainee, "score.ready")
        state = hub.get(session_id)
        metric = next(item for item in state.score["metrics"] if item["key"] == "card_fill_time")
        assert not metric["passed"]
        assert "61 с" in metric["fact"] and "+1 с" in metric["fact"]
        assert metric["norm"] == "сдать карточку за 60 с"
        finding = next(item for item in state.score["findings"] if item["code"] == "E3")
        assert "Время заполнения карточки" in finding["summary"]
    finally:
        control.__exit__(None, None, None)


def test_grammar_criterion_flags_incorrect_card_description(client, monkeypatch):
    monkeypatch.setattr("app.session.finish.assess", rules_only_grammar)
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            read_until(trainee, "card.briefing")
            trainee.send_json({"type": "kio.patch", "fields": {
                "address": "улица Ленина, 14", "floor": "5", "incident_type": "fire",
                "victims_count": 2, "description": "горит балкон",
                "signs": ["жилой дом", "балкон", "открытое пламя"],
            }})
            wait_for(lambda: hub.get(session_id).kio.incident_code)
            trainee.send_json({"type": "card.submit"})
            read_until(trainee, "call.ended")
            read_until(trainee, "score.ready")

        score = wait_for(lambda: hub.get(session_id).score)
        metric = next(item for item in score["metrics"] if item["key"] == "description_grammar")
        assert not metric["passed"]
        assert metric["fact"]
        assert any(item["code"] == "E4" for item in score["findings"])
        e4 = next(item for item in score["findings"] if item["code"] == "E4")
        assert e4["source"] == "grammar"
        assert score["score_auto"] < 100
    finally:
        control.__exit__(None, None, None)


def test_disabled_grammar_criterion_does_not_change_card_score(client, monkeypatch):
    monkeypatch.setattr("app.session.finish.assess", rules_only_grammar)
    session_id, control = start(client, criteria={"require_correct_grammar": False})
    try:
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            read_until(trainee, "card.briefing")
            trainee.send_json({"type": "kio.patch", "fields": {
                "address": "улица Ленина, 14", "floor": "5", "incident_type": "fire",
                "victims_count": 2, "description": "горит балкон",
                "signs": ["жилой дом", "балкон", "открытое пламя"],
            }})
            wait_for(lambda: hub.get(session_id).kio.incident_code)
            trainee.send_json({"type": "card.submit"})
            read_until(trainee, "call.ended")
            read_until(trainee, "score.ready")

        score = wait_for(lambda: hub.get(session_id).score)
        assert score["score_auto"] == 100
        assert not any(item["key"] == "description_grammar" for item in score["metrics"])
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


def test_filled_112_card_continues_as_dds_in_the_same_lesson(client):
    session_id, control = start(client, handoff_to_dds=True)
    try:
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            briefing = read_until(trainee, "card.briefing")
            assert briefing["handoff_to_dds"] is True
            trainee.send_json({"type": "kio.patch", "fields": {
                "address": "улица Ленина, 14", "incident_type": "fire",
                "victims_count": 2, "description": "горит балкон",
                "signs": ["жилой дом", "балкон", "открытое пламя"],
            }})
            wait_for(lambda: hub.get(session_id).kio.incident_code)
            trainee.send_json({"type": "card.submit"})
            assert read_until(trainee, "call.ended")["reason"] == "complete"
            state = hub.get(session_id)
            assert state.dispatched_card is not None and not state.ended
            assert state.score is None
            trainee.send_json({"type": "kio.patch", "fields": {"address": "поздняя правка"}})
            assert "передана" in read_until(trainee, "error")["message"]

        with client.websocket_connect(f"/ws/call/{session_id}") as late:
            assert read_until(late, "card.briefing")["card"]["address"] == "улица Ленина, 14"
            assert read_until(late, "call.ended")["reason"] == "complete"

        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            assert read_until(station, "card.received")["card"]["address"] == "улица Ленина, 14"
            snapshot = read_until(station, "station.state")["snapshot"]
            service = snapshot["services"][0]
            crew = next(item for item in snapshot["crew_options"] if item.startswith(service + " — "))
            station.send_json({"type": "card.status", "service": service, "status": "accepted",
                               "comment": "Старший группы подтвердил приём карточки."})
            read_until(station, "station.state")
            station.send_json({"type": "crew.select", "crew": crew})
            read_until(station, "station.state")
            station.send_json({"type": "phone.dial"})
            assert read_until(station, "phone.line")["speaker"] == "crew"
            read_until(station, "station.state")
            station.send_json({"type": "phone.brief", "address": "улица Ленина, 14",
                               "incident": "Пожар в квартире",
                               "request": "Прошу подтвердить выезд и доложить о прибытии"})
            assert read_until(station, "phone.report")["phase"] == "dispatched"
            read_until(station, "station.state")
            station.send_json({"type": "card.status", "service": service, "status": "responding",
                               "comment": "Старший группы сообщил о начале реагирования."})
            read_until(station, "station.state")
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")

        score = wait_for(lambda: hub.get(session_id).score)
        assert hub.get(session_id).ended
        keys = {item["key"] for item in score["metrics"]}
        assert {"address", "incident_signs", "dds_primary", "dds_progress"} <= keys
        assert "dds_contact" not in keys
        assert score["score_auto"] < 100
    finally:
        control.__exit__(None, None, None)


def test_handoff_queue_mixes_trainee_card_then_selected_prepared_card(client):
    session_id, control = start(
        client, handoff_to_dds=True,
        scenario_ids=["fire-apartment-l2", "t01-1-fire-container"],
    )
    try:
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            read_until(trainee, "card.briefing")
            trainee.send_json({"type": "kio.patch", "fields": {
                "address": "улица Ленина, 14", "incident_type": "fire",
                "victims_count": 2, "description": "горит балкон",
                "signs": ["жилой дом", "балкон", "открытое пламя"],
            }})
            wait_for(lambda: hub.get(session_id).kio.incident_code)
            trainee.send_json({"type": "card.submit"})
            read_until(trainee, "call.ended")

        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            first = read_until(station, "card.received")
            assert first["from_operator"] == "Иванов"
            assert first["card"]["address"] == "улица Ленина, 14"
            snapshot = read_until(station, "station.state")["snapshot"]
            assert snapshot["card_total"] == 2
            assert {item["scenario_id"] for item in snapshot["queue_cards"]} == {
                "fire-apartment-l2", "t01-1-fire-container"
            }
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")

        state = wait_for(lambda: hub.get(session_id).score)
        assert state["card_results"]
        assert [item["scenario_id"] for item in state["card_results"]] == [
            "fire-apartment-l2", "t01-1-fire-container"
        ]
        assert {item["key"] for item in state["metrics"]} >= {
            "address", "incident_signs", "dds_primary"
        }
        assert state["score_auto"] < 100
    finally:
        control.__exit__(None, None, None)


def test_model_coaching_reaches_report_without_changing_score(client, monkeypatch):
    from app.domain.events import AICoaching, AIRecommendation

    seen: list[list[str]] = []

    async def fake_coach(metrics):
        failed = [item.key for item in metrics if not item.passed and item.weight > 0]
        seen.append(failed)
        return AICoaching(status="ready", model="test", recommendations=[
            AIRecommendation(metric_key=failed[0], text="Перечитайте описание перед сдачей карточки."),
        ])

    monkeypatch.setattr("app.session.finish.assess", rules_only_grammar)
    monkeypatch.setattr("app.session.finish.coach", fake_coach)
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            read_until(trainee, "card.briefing")
            trainee.send_json({"type": "kio.patch", "fields": {
                "address": "улица Ленина, 14", "floor": "5", "incident_type": "fire",
                "victims_count": 2, "description": "горит балкон",
                "signs": ["жилой дом", "балкон", "открытое пламя"],
            }})
            wait_for(lambda: hub.get(session_id).kio.incident_code)
            trainee.send_json({"type": "card.submit"})
            read_until(trainee, "call.ended")
            read_until(trainee, "score.ready")

        score = wait_for(lambda: hub.get(session_id).score)
        assert seen and "description_grammar" in seen[0]
        coaching = score["full_report"]["ai_coaching"]
        assert coaching["status"] == "ready"
        assert coaching["recommendations"][0]["metric_key"] == seen[0][0]
        assert score["score_auto"] < 100
    finally:
        control.__exit__(None, None, None)
