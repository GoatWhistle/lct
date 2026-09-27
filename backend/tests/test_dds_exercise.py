"""Готовая карточка → учебный звонок бригаде → числовая оценка ДДС."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.http import sessions as sessions_http
from app.api.ws.control import _start
from app.config import get_settings
from app.db.base import get_session
from app.domain.events import Exercise, ScenarioStart, SessionMode
from app.domain.timers import TimerCode
from app.main import app
from app.scenarios import store
from app.session.hub import hub
from app.session.checkpoint import dump_state, load_state
from app.session.store import MemorySessionStore


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    get_settings.cache_clear()

    async def session_override():
        # These tests exercise live in-memory sessions; no endpoint below needs
        # persistence, but the report route still requires its DB dependency.
        yield object()

    async def audit_override(*_args, **_kwargs):
        return None

    monkeypatch.setitem(app.dependency_overrides, get_session, session_override)
    async def optional_session_override():
        yield None

    monkeypatch.setitem(
        app.dependency_overrides,
        sessions_http.optional_session,
        optional_session_override,
    )
    monkeypatch.setattr(sessions_http, "audit_required", audit_override)
    try:
        with TestClient(app) as test_client:
            test_client.post("/api/auth/dev-token")
            hub.store = MemorySessionStore()
            yield test_client
    finally:
        get_settings.cache_clear()


def wait_for(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("состояние не обновилось")


def read_until(socket, wanted):
    received = []
    for _ in range(20):
        event = socket.receive_json()
        received.append(event["type"])
        if event["type"] == wanted:
            return event
    raise AssertionError(f"событие {wanted} не пришло; получены: {received}")


def start(
    client,
    exercise="dds",
    criteria=None,
    dds_service=None,
    scenario_id="fire-apartment-l2",
    random_scenario_ids=None,
):
    session_id = uuid4()
    context = client.websocket_connect(f"/ws/control/{session_id}")
    control = context.__enter__()
    payload = {
        "type": "scenario.start",
        "scenario_id": scenario_id,
        "trainee": "Иванов",
        "mode": "training",
        "exercise": exercise,
    }
    if criteria is not None:
        payload["criteria"] = criteria
    if dds_service is not None:
        payload["dds_service"] = dds_service
    if random_scenario_ids is not None:
        payload["random_scenario_ids"] = random_scenario_ids
    control.send_json(payload)
    wait_for(lambda: hub.get(session_id))
    return session_id, context


def complete_phone_call(station, state, expected_phase):
    station.send_json({"type": "phone.dial"})
    greeting = read_until(station, "phone.line")
    assert greeting["speaker"] == "crew"
    read_until(station, "station.state")
    if expected_phase == "dispatched":
        station.send_json(
            {
                "type": "phone.brief",
                "address": state.desk.active.dispatched_card.address,
                "incident": state.desk.active.scenario.title,
                "request": "Прошу подтвердить выезд и сообщить о прибытии",
            }
        )
    else:
        station.send_json(
            {"type": "phone.check", "text": "Сообщите текущую обстановку по карточке"}
        )
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
        assert state.desk.active.dispatched_card is not None
        assert state.desk.active.kio.address == "улица Ленина, 14"
        assert state.desk.active.kio.notify
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


def test_repeated_ack_and_crew_selection_do_not_duplicate_dds_log(client):
    session_id, control = start(client)
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            crew = snapshot["crew_options"][0]

            station.send_json({"type": "card.ack", "comment": "Основание: карточка передана диспетчеру."})
            read_until(station, "station.state")
            station.send_json({"type": "card.ack", "comment": "Основание: карточка передана диспетчеру."})
            station.send_json({"type": "crew.select", "crew": crew})
            read_until(station, "station.state")
            assert sum(action == "card.ack" for action, *_ in state.desk.active.dds_log) == 1

            station.send_json({"type": "crew.select", "crew": crew})
            station.send_json({"type": "zone.decision", "in_zone": True})
            read_until(station, "station.state")
            assert sum(action == "crew.select" for action, *_ in state.desk.active.dds_log) == 1
    finally:
        control.__exit__(None, None, None)


def test_zone_decision_is_one_shot_and_restored_in_station_snapshot(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            initial = read_until(station, "station.state")["snapshot"]
            assert initial["zone_decision"] is None

            station.send_json({"type": "zone.decision", "in_zone": True})
            accepted = read_until(station, "station.state")["snapshot"]
            assert accepted["zone_decision"] is True
            state = hub.get(session_id)
            assert sum(action == "zone.decision" for action, *_ in state.desk.active.dds_log) == 1

            station.send_json({"type": "zone.decision", "in_zone": True})
            assert sum(action == "zone.decision" for action, *_ in state.desk.active.dds_log) == 1
            station.send_json({"type": "zone.decision", "in_zone": False})
            error = read_until(station, "error")
            assert "уже записано" in error["message"]
            assert sum(action == "zone.decision" for action, *_ in state.desk.active.dds_log) == 1

            station.close()
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            restored = read_until(station, "station.state")["snapshot"]
            assert restored["zone_decision"] is True
    finally:
        control.__exit__(None, None, None)


def test_server_randomly_selects_only_from_instructor_filtered_card_pool(
    client, monkeypatch
):
    pool = ["fire-apartment-l2", "t01-1-fire-container"]
    monkeypatch.setattr(
        "app.api.ws.control.secrets.choice", lambda scenarios: scenarios[-1]
    )
    session_id, control = start(client, random_scenario_ids=pool)
    try:
        state = hub.get(session_id)
        assert state.desk.active.scenario.id == "t01-1-fire-container"
        assert state.desk.active.dispatched_card is not None
        assert [item.id for item in state.desk.scenarios] == pool[::-1]
        assert state.desk.next_index == 2
        assert state.desk.next_arrival_at is None
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


@pytest.mark.parametrize("scenario_id", [
    "t01-3-child-other-region",
    "t02-2-megafon-consultation",
    "t07-2-headache-ryazan",
    "t11-3-lost-in-forest",
    "t12-2-heart-pain",
    "t16-2-child-bicycle-volzhsky",
    "t19-1-field-fire",
    "t19-2-snake-bite",
    "t27-3-wall-crack",
    "t29-2-accident-fight",
])
def test_dds_rejects_non_card_outcomes_instead_of_making_fake_cards(
    client, monkeypatch, scenario_id
):
    scenario = store.get(scenario_id)
    assert scenario is not None and scenario.outcome.value in {"consultation", "transfer_region"}
    emitted = []
    monkeypatch.setattr(
        hub, "to_observers", lambda session_id, event: emitted.append(event)
    )
    session_id = uuid4()
    event = ScenarioStart(
        scenario_id=scenario.id,
        scenario_ids=[scenario.id],
        trainee="Иванов",
        mode=SessionMode.TRAINING,
        exercise=Exercise.DDS,
    )

    asyncio.run(_start(session_id, event))

    assert hub.get(session_id) is None
    assert emitted[-1].code.value == "scenario_invalid"
    assert "готовые карточки" in emitted[-1].message


def test_instructor_live_registry_shows_owned_dds_session_and_deadline_state(client):
    session_id, control = start(client)
    try:
        response = client.get("/api/sessions/active")
        assert response.status_code == 200
        rows = response.json()
        row = next(item for item in rows if item["session_id"] == str(session_id))
        assert row["exercise"] == "dds"
        assert row["scenario_title"]
        assert row["dds_card_total"] == 1
        assert row["dds_open_cards"] == 1
        assert row["dds_overdue_cards"] == 0
        assert row["dds_snapshot"]["queue_cards"][0]["active"] is True
        assert row["dds_snapshot"]["queue_cards"][0]["title"]
        assert row["dds_snapshot"]["queue_cards"][0]["service_status"] == "added"
        assert row["dds_snapshot"]["phone_reports"] == []
        state = hub.get(session_id)
        live_card = state.desk.ordered()[0]
        live_card.timers.on_event("dds.open")
        live_card.timers.timers[TimerCode.DDS_WORK].started_at = time.monotonic() - 181
        overdue_response = client.get("/api/sessions/active")
        overdue_row = next(
            item
            for item in overdue_response.json()
            if item["session_id"] == str(session_id)
        )
        assert overdue_row["dds_work_overdue_cards"] == 1
        assert row["dds_statuses"]
    finally:
        control.__exit__(None, None, None)


def test_instructor_live_registry_includes_current_crew_report(client, monkeypatch):
    from app.api import auth

    async def keep_test_auth_state_fresh():
        auth.prime_generations({})

    auth.prime_generations({})
    monkeypatch.setattr(auth, "sync_generations", keep_test_auth_state_fresh)
    session_id, control = start(client)
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}?role=dds") as station:
            read_until(station, "station.state")
            crew = state.crew_options(state.desk.active)[0]
            station.send_json({"type": "crew.select", "crew": crew})
            read_until(station, "station.state")
            service = state.crew_service(state.desk.active, crew)
            station.send_json({
                "type": "card.status", "service": service, "status": "accepted",
                "comment": "Основание: профиль полномочий ДДС.\nСведения: карточка принята.",
            })
            read_until(station, "station.state")
            report = complete_phone_call(station, state, "dispatched")

        rows = client.get("/api/sessions/active").json()
        row = next(item for item in rows if item["session_id"] == str(session_id))
        saved_report = row["dds_snapshot"]["phone_reports"][0]
        assert saved_report["crew"] == report["crew"]
        assert saved_report["phase"] == "dispatched"
        assert saved_report["text"] == report["text"]
    finally:
        control.__exit__(None, None, None)


def test_ticket_dds_card_uses_source_caller_identity_and_phone(client):
    session_id, control = start(client, scenario_id="t01-1-fire-container")
    try:
        state = hub.get(session_id)
        assert state.desk.active.kio.caller_name == "Сидоров Иван Сергеевич"
        assert state.desk.active.kio.caller_number == "916-126-34-71"
        assert state.desk.active.kio.caller_contact == "916-126-34-71"
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
        assert state.desk.active.kio.caller_name is None
        assert state.desk.active.kio.caller_number == "916-123-98-78"
        assert state.desk.active.kio.caller_contact == "916-123-98-78"
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
            station.send_json(
                {
                    "type": "card.status",
                    "service": "Служба 101",
                    "status": "accepted",
                }
            )
            assert "только своей ДДС" in read_until(station, "error")["message"]
            station.send_json(
                {
                    "type": "card.status",
                    "service": "МВД",
                    "status": "accepted",
                    "comment": "Основание: доклад старшего.\nСведения: карточка для нашей службы.",
                }
            )
            assert (
                read_until(station, "station.state")["snapshot"]["statuses"]["МВД"]
                == "accepted"
            )
    finally:
        control.__exit__(None, None, None)


def test_instructor_criteria_change_timer_and_success_threshold(client, monkeypatch):
    from app.api import auth

    async def keep_test_auth_state_fresh():
        auth.prime_generations({})

    auth.prime_generations({})
    monkeypatch.setattr(auth, "sync_generations", keep_test_auth_state_fresh)
    session_id, control = start(
        client,
        criteria={
            "decision_time_limit_seconds": 45,
            "dds_report_reaction_limit_seconds": 20,
            "allowed_errors": 50,
            "require_correct_grammar": False,
            "score_weights": {"dds_ack": 3.5},
        },
    )
    try:
        state = hub.get(session_id)
        assert state.criteria.decision_time_limit_seconds == 45
        assert state.timers.limits[TimerCode.DDS_ACK] == 45_000
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            card = read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            service = snapshot["services"][0]
            station.send_json(
                {"type": "card.status", "service": service, "status": "accepted",
                 "comment": "Основание: профиль полномочий ДДС.\nСведения: карточка принята."}
            )
            read_until(station, "station.state")
            station.send_json(
                {
                    "type": "card.reply",
                    "card_id": card["card"]["card_id"],
                    "text": "Сообщение приняты, бригада направлено.",
                }
            )
            read_until(station, "station.state")
            # Simulate the acknowledgement being lost during reconnect. The
            # buffered replacement may be replayed, but its journal is unique.
            station.send_json(
                {
                    "type": "card.reply",
                    "card_id": card["card"]["card_id"],
                    "text": "Сообщение приняты, бригада направлено.",
                }
            )
            read_until(station, "station.state")
            assert len(state.desk.active.reply_log) == 1
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")

        score = wait_for(lambda: hub.get(session_id).score)
        ack = next(item for item in score["metrics"] if item["key"] == "dds_ack")
        assert ack["norm"] == "≤ 45 с"
        assert ack["weight"] == 3.5
        reply_metric = next(item for item in score["metrics"] if item["key"] == "dds_reply")
        assert reply_metric["passed"]
        assert not any(item["key"] == "dds_grammar" for item in score["metrics"])
        # 3 минуты — до первой записи, а она сделана сразу: незавершённая
        # работа по времени не штрафуется (ответ заказчика, П.5).
        assert (
            next(item for item in score["metrics"] if item["key"] == "dds_work_time")[
                "passed"
            ]
            is True
        )
        report = client.get(f"/api/sessions/{session_id}/report").json()
        assert report["criteria"] == {
            "decision_time_limit_seconds": 45,
            "card_fill_time_limit_seconds": 180,
            "dds_card_work_time_limit_seconds": 180,
            "dds_report_reaction_enabled": False,
            "dds_report_reaction_limit_seconds": 20,
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
            station.send_json(
                {"type": "card.status", "service": service, "status": "accepted",
                 "comment": "Основание: профиль полномочий ДДС.\nСведения: карточка принята."}
            )
            read_until(station, "station.state")
            station.send_json(
                {"type": "card.status", "service": service, "status": "responding",
                 "comment": "Основание: доклад старшего.\nСведения: начало реагирования подтверждено."}
            )
            assert (
                read_until(station, "station.state")["snapshot"]["statuses"][service]
                == "responding"
            )
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")
        score = wait_for(lambda: hub.get(session_id).score)
        keys = {metric["key"] for metric in score["metrics"]}
        assert "dds_primary" in keys and "dds_progress" in keys
        crew_metric = next(
            metric for metric in score["metrics"] if metric["key"] == "dds_crew"
        )
        assert not crew_metric["passed"]
        assert "dds_contact" not in keys
        assert "answer_time" not in keys and "interview_time" not in keys
        assert 0 < score["score_auto"] < 100
        assert any(
            finding["code"] == "D2" and "бригады" in finding["summary"]
            for finding in score["findings"]
        )
        assert not any(finding["code"] == "E3" for finding in score["findings"]), (
            "первая запись сделана сразу: дальше работы по времени не нормируются"
        )
        finding_codes = {finding["code"] for finding in score["findings"]}
        penalty_codes = {
            "dds_primary": {"D1"},
            "dds_ack": {"D1"},
            "dds_decision": {"D2", "D3"},
            "dds_crew": {"D2"},
            "dds_progress": {"D6"},
            "dds_completion": {"D6"},
            "dds_reply": {"D5"},
            "dds_work_time": {"E3"},
        }
        unexplained = [
            metric["key"]
            for metric in score["metrics"]
            if not metric["passed"]
            and not penalty_codes.get(metric["key"], set()).intersection(finding_codes)
        ]
        assert not unexplained, f"проваленные метрики без кода и пояснения: {unexplained}"
    finally:
        control.__exit__(None, None, None)


def test_dds_d5_comment_is_explanatory_and_does_not_change_numeric_score(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            card = read_until(station, "card.received")["card"]
            snapshot = read_until(station, "station.state")["snapshot"]
            service = snapshot["services"][0]
            crew = next(
                option
                for option in snapshot["crew_options"]
                if option.startswith(service + " — ")
            )
            station.send_json(
                {"type": "card.status", "service": service, "status": "accepted",
                 "comment": "Основание: профиль полномочий ДДС.\nСведения: карточка принята."}
            )
            read_until(station, "station.state")
            station.send_json({"type": "crew.select", "crew": crew})
            read_until(station, "station.state")
            for status in ("responding", "arrived", "working", "completed"):
                station.send_json(
                    {"type": "card.status", "service": service, "status": status,
                     "comment": f"Основание: доклад ответственной службы.\nСведения: этап {status}."}
                )
                read_until(station, "station.state")
            station.send_json(
                {
                    "type": "card.reply",
                    "card_id": card["card_id"],
                    "text": "Все принято.",
                }
            )
            read_until(station, "station.state")
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")
        score = wait_for(lambda: hub.get(session_id).score)
        assert score["score_auto"] == 100.0
        d5 = next(finding for finding in score["findings"] if finding["code"] == "D5")
        assert "получателя" in d5["summary"]
    finally:
        control.__exit__(None, None, None)


def test_phone_dial_requires_brief_and_rejects_wrong_card_facts(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            service = snapshot["services"][0]
            crew = next(
                option
                for option in snapshot["crew_options"]
                if option.startswith(service + " — ")
            )
            station.send_json(
                {"type": "card.status", "service": service, "status": "accepted",
                 "comment": "Основание: доклад старшего.\nСведения: карточка принята."}
            )
            read_until(station, "station.state")
            station.send_json({"type": "crew.select", "crew": crew})
            read_until(station, "station.state")
            station.send_json({"type": "phone.dial"})
            assert read_until(station, "phone.line")["speaker"] == "crew"
            pending = read_until(station, "station.state")["snapshot"]
            assert pending["phone_pending"]["phase"] == "dispatched"
            assert not pending["phone_reports"]
            station.send_json(
                {"type": "card.status", "service": service, "status": "responding",
                 "comment": "Основание: доклад старшего.\nСведения: начало реагирования подтверждено."}
            )
            assert (
                read_until(station, "station.state")["snapshot"]["statuses"][service]
                == "responding"
            )
            station.send_json(
                {
                    "type": "phone.brief",
                    "address": "другая улица, дом 99",
                    "incident": "Пожар в квартире",
                    "request": "Прошу направить бригаду",
                }
            )
            assert "адрес" in read_until(station, "error")["message"]
            station.send_json(
                {
                    "type": "phone.brief",
                    "address": "улица Ленина, 14",
                    "incident": "Ничего не произошло",
                    "request": "Прошу направить бригаду",
                }
            )
            assert "характер" in read_until(station, "error")["message"]
            station.send_json({"type": "phone.hangup"})
            assert (
                read_until(station, "station.state")["snapshot"]["phone_pending"]
                is None
            )
            assert not hub.get(session_id).desk.active.phone_reports
            complete_phone_call(station, hub.get(session_id), "dispatched")
            read_until(station, "station.state")
            assert len(hub.get(session_id).desk.active.phone_reports) == 1
            station.send_json({"type": "phone.dial"})
            read_until(station, "phone.line")
            read_until(station, "station.state")
            station.send_json(
                {"type": "phone.check", "text": "Здравствуйте, хорошая погода"}
            )
            assert "обстановку" in read_until(station, "error")["message"]
            assert len(hub.get(session_id).desk.active.phone_reports) == 1
    finally:
        control.__exit__(None, None, None)


def test_default_exercise_remains_call(client):
    session_id = uuid4()
    with client.websocket_connect(f"/ws/control/{session_id}") as control:
        control.send_json(
            {
                "type": "scenario.start",
                "scenario_id": "fire-apartment-l2",
                "trainee": "Иванов",
                "mode": "training",
            }
        )
        state = wait_for(lambda: hub.get(session_id))
        assert state.exercise.value == "call"
        assert state.dispatched_card is None


def test_complete_dds_workflow_scores_100_with_assignment_without_call(client):
    session_id, control = start(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            for service in snapshot["services"]:
                station.send_json(
                    {"type": "card.status", "service": service, "status": "accepted",
                     "comment": "Основание: доклад старшего.\nСведения: карточка принята."}
                )
                read_until(station, "station.state")
                crew = next(
                    option
                    for option in snapshot["crew_options"]
                    if option.startswith(service + " — ")
                )
                station.send_json({"type": "crew.select", "crew": crew})
                snapshot = read_until(station, "station.state")["snapshot"]
                for status in ("responding", "arrived", "working", "completed"):
                    station.send_json(
                        {"type": "card.status", "service": service, "status": status,
                         "comment": f"Основание: доклад старшего.\nСведения: этап {status}."}
                    )
                    snapshot = read_until(station, "station.state")["snapshot"]
            assert snapshot["card"] == "completed"
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")
        score = wait_for(lambda: hub.get(session_id).score)
        assert score["score_auto"] == 100.0
        assert not score["findings"]
        assert all(metric["key"].startswith("dds_") for metric in score["metrics"])
        crew_metric = next(
            metric for metric in score["metrics"] if metric["key"] == "dds_crew"
        )
        assert crew_metric["passed"]
        assert next(
            metric for metric in score["metrics"] if metric["key"] == "dds_work_time"
        )["passed"]
        assert not any(
            metric["key"] in {"dds_contact", "dds_grammar"}
            for metric in score["metrics"]
        )
    finally:
        control.__exit__(None, None, None)


def test_late_first_record_has_e3_and_long_work_after_it_does_not(client):
    """П.5: 3 минуты — на открытие и первую запись; дальше работы идут часы."""
    session_id, control = start(
        client, criteria={"dds_card_work_time_limit_seconds": 60}
    )
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            received = read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            service = snapshot["services"][0]
            station.send_json({"type": "card.open", "card_id": received["card"]["card_id"]})
            read_until(station, "station.state")
            assert state.desk.active.timers.measured_ms(TimerCode.DDS_ACK) is not None
            state.desk.active.timers.timers[TimerCode.DDS_WORK].started_at = time.monotonic() - 61
            station.send_json(
                {"type": "card.status", "service": service, "status": "accepted",
                 "comment": "Основание: доклад старшего.\nСведения: карточка принята."}
            )
            snapshot = read_until(station, "station.state")["snapshot"]
            crew = next(
                option
                for option in snapshot["crew_options"]
                if option.startswith(service + " — ")
            )
            station.send_json({"type": "crew.select", "crew": crew})
            read_until(station, "station.state")
            for status in ("responding", "arrived", "working", "completed"):
                station.send_json(
                    {"type": "card.status", "service": service, "status": status,
                     "comment": f"Основание: доклад старшего.\nСведения: этап {status}."}
                )
                read_until(station, "station.state")
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")
        score = wait_for(lambda: state.score)
        metric = next(
            item for item in score["metrics"] if item["key"] == "dds_work_time"
        )
        assert not metric["passed"]
        assert "61 с" in metric["fact"] and "+1 с" in metric["fact"]
        findings = [item for item in score["findings"] if item["code"] == "E3"]
        assert len(findings) == 1 and "первая запись" in findings[0]["summary"]
    finally:
        control.__exit__(None, None, None)


def test_complete_dds_workflow_with_training_calls_and_status_updates(client):
    session_id, control = start(client, criteria={"dds_report_reaction_enabled": True})
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            service = snapshot["services"][0]
            crew = next(
                option
                for option in snapshot["crew_options"]
                if option.startswith(service + " — ")
            )

            station.send_json(
                {"type": "card.status", "service": service, "status": "accepted",
                 "comment": "Основание: доклад старшего.\nСведения: карточка принята."}
            )
            read_until(station, "station.state")
            station.send_json({"type": "crew.select", "crew": crew})
            read_until(station, "station.state")
            complete_phone_call(station, state, "dispatched")
            read_until(station, "station.state")

            for status, phase in (
                ("responding", "arrived"),
                ("arrived", "working"),
                ("working", "completed"),
                ("completed", None),
            ):
                station.send_json(
                    {"type": "card.status", "service": service, "status": status,
                     "comment": f"Основание: доклад старшего.\nСведения: этап {status}."}
                )
                read_until(station, "station.state")
                if phase:
                    complete_phone_call(station, state, phase)
                    read_until(station, "station.state")

            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")
        score = wait_for(lambda: state.score)
        assert score["score_auto"] == 100.0
        assert [report.phase for report in state.desk.active.phone_reports] == [
            "dispatched",
            "arrived",
            "working",
            "completed",
        ]
        assert all(report.crew == crew for report in state.desk.active.phone_reports)
        assert not score["findings"]
        reaction = [m for m in score["metrics"] if m["key"] == "dds_report_reaction"]
        assert len(reaction) == 1 and reaction[0]["passed"], "каждый доклад отмечен вовремя"
        assert "с нарушением 0 из 4 докладов" in reaction[0]["fact"]
    finally:
        control.__exit__(None, None, None)


STATUS_COMMENT = "Основание: доклад бригады.\nСведения: передано старшему бригады."


def accept_with_crew(station):
    read_until(station, "card.received")
    snapshot = read_until(station, "station.state")["snapshot"]
    service = snapshot["services"][0]
    crew = next(option for option in snapshot["crew_options"]
                if option.startswith(service + " — "))
    station.send_json({"type": "card.status", "service": service, "status": "accepted",
                       "comment": STATUS_COMMENT})
    read_until(station, "station.state")
    station.send_json({"type": "crew.select", "crew": crew})
    read_until(station, "station.state")
    return service


def send_status(station, service, status):
    station.send_json({"type": "card.status", "service": service, "status": status,
                       "comment": STATUS_COMMENT})
    read_until(station, "station.state")


def test_crew_discovers_card_address_error_and_dispatcher_reports_it_to_112(client):
    session_id, control = start(client, scenario_id="dds-card-address-error")
    try:
        state = hub.get(session_id)
        card = state.desk.active
        assert card.dispatched_card.address == "улица Ленина, 16"
        assert card.scenario.ground_truth.address == "улица Ленина, 14"
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            service = accept_with_crew(station)
            station.send_json({
                "type": "card.error_report_112", "card_id": str(card.card_id),
                "text": "В карточке неверный адрес",
            })
            assert "Сначала получите доклад" in read_until(station, "error")["message"]
            complete_phone_call(station, state, "dispatched")
            read_until(station, "station.state")
            send_status(station, service, "responding")
            report = complete_phone_call(station, state, "arrived")
            snapshot = read_until(station, "station.state")["snapshot"]
            assert "улица Ленина, 14" in report["text"]
            assert "улица Ленина, 16" in report["text"]
            assert snapshot["card_error_discovered"] is True
            assert snapshot["report_112_text"] is None

            message = "В карточке дом 16; бригада на месте установила дом 14."
            station.send_json({
                "type": "card.error_report_112", "card_id": str(card.card_id), "text": message,
            })
            snapshot = read_until(station, "station.state")["snapshot"]
            assert snapshot["report_112_text"] == message
            station.send_json({
                "type": "card.error_report_112", "card_id": str(card.card_id), "text": message,
            })
            assert sum(action == "card.error_report_112" for action, _, _ in card.dds_log) == 1

            active = client.get("/api/sessions/active").json()
            teacher = next(row for row in active if row["session_id"] == str(session_id))
            assert teacher["dds_snapshot"]["report_112_text"] == message
            restored = load_state(dump_state(state), datetime.now(UTC))
            assert restored.station_snapshot().report_112_text == message
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")

        report = client.get(f"/api/sessions/{session_id}/report").json()
        actions = report["card_results"][0]["actions"]
        assert any(action["type"] == "card.error_report_112" and action["text"] == message
                   for action in actions)
        assert message in client.get(f"/api/sessions/{session_id}/report.csv").text
    finally:
        control.__exit__(None, None, None)


def test_report_to_112_is_unavailable_without_planted_card_error(client):
    session_id, control = start(client)
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            snapshot = read_until(station, "station.state")["snapshot"]
            assert snapshot["card_error_discovered"] is False
            station.send_json({
                "type": "card.error_report_112", "card_id": str(state.desk.active.card_id),
                "text": "Сообщаю об ошибке карточки в 112",
            })
            assert "Сначала получите доклад" in read_until(station, "error")["message"]
            assert not state.desk.active.dds_log
    finally:
        control.__exit__(None, None, None)


@pytest.mark.parametrize(("reaction", "passed"), [
    ("fast", True), ("slow", False), ("missing", False), ("early", True),
])
def test_dds_report_reaction_goes_from_criteria_to_report(client, reaction, passed):
    """Лимит из `scenario.start` доходит до метрики, отметки D6 и отчёта."""
    session_id, control = start(client, criteria={
        "dds_report_reaction_enabled": True,
        "dds_report_reaction_limit_seconds": 20,
        "score_weights": {"dds_report_reaction": 3.5},
    })
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            service = accept_with_crew(station)
            if reaction == "early":
                send_status(station, service, "responding")
            complete_phone_call(station, state, "dispatched")
            read_until(station, "station.state")
            if reaction in {"fast", "slow"}:
                send_status(station, service, "responding")
            if reaction == "slow":
                # Доклад на 70 с раньше статуса: время доклада — серверное.
                state.desk.active.phone_reports[0].at -= timedelta(seconds=70)
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")

        score = wait_for(lambda: hub.get(session_id).score)
        metrics = [item for item in score["metrics"] if item["key"] == "dds_report_reaction"]
        assert len(metrics) == 1
        metric = metrics[0]
        assert metric["passed"] is passed
        assert metric["weight"] == 3.5
        assert metric["norm"] == "отметить статус по докладу за 20 с"
        assert "«Начало реагирования» по докладу бригады" in metric["fact"]
        findings = [item for item in score["findings"]
                    if item["metric_key"] == "dds_report_reaction"]
        assert [item["code"] for item in findings] == ([] if passed else ["D6"])
        assert all(item["competency"] == "norms" for item in findings)
        if reaction == "slow":
            assert "70 с (+50 с сверх норматива)" in metric["fact"]
            assert findings[0]["fact"] == metric["fact"]
        if reaction == "missing":
            assert metric["fact"].endswith(": не отмечен") and metric["credit"] == 0.0
        if reaction == "early":
            assert metric["fact"].endswith("отмечен не позже доклада")

        report = client.get(f"/api/sessions/{session_id}/report").json()
        assert report["criteria"]["dds_report_reaction_limit_seconds"] == 20
        assert report["criteria"]["dds_report_reaction_enabled"] is True
        assert [item for item in report["metrics"]
                if item["key"] == "dds_report_reaction"] == metrics
        if reaction == "slow":
            csv_text = client.get(f"/api/sessions/{session_id}/report.csv").content.decode("utf-8-sig")
            assert "Лимит реакции на доклад бригады" in csv_text
            assert "70 с (+50 с сверх норматива)" in csv_text
    finally:
        control.__exit__(None, None, None)


def test_refusal_after_crew_report_does_not_lower_score_without_finding(client):
    """Бригада доложила о прибытии, но работы не проводились: отказ с причиной
    — терминальный исход, неотмеченное «Прибытие» не штрафуется."""
    session_id, control = start(client, criteria={"dds_report_reaction_enabled": True})
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            service = accept_with_crew(station)
            complete_phone_call(station, state, "dispatched")
            read_until(station, "station.state")
            send_status(station, service, "responding")
            complete_phone_call(station, state, "arrived")
            read_until(station, "station.state")
            send_status(station, service, "refused")
            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")

        score = wait_for(lambda: hub.get(session_id).score)
        assert [report.phase for report in state.desk.active.phone_reports] == [
            "dispatched", "arrived",
        ]
        metric = next(item for item in score["metrics"] if item["key"] == "dds_report_reaction")
        assert metric["passed"], "прибытие при отказе не требуется"
        assert "о выезде" in metric["fact"] and "докладов" not in metric["fact"]
        assert not [item for item in score["findings"] if item["code"] == "D6"]
    finally:
        control.__exit__(None, None, None)


def test_concurrent_dds_cards_keep_independent_state_and_timers(client):
    session_id = uuid4()
    control_ctx = client.websocket_connect(f"/ws/control/{session_id}")
    control = control_ctx.__enter__()
    control.send_json(
        {
            "type": "scenario.start",
            "scenario_id": "fire-apartment-l2",
            "scenario_ids": ["fire-apartment-l2", "t20-2-stroke"],
            "trainee": "Иванов",
            "mode": "training",
            "exercise": "dds",
        }
    )
    state = wait_for(lambda: hub.get(session_id))
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
            first_live = next(
                item
                for item in state.desk.cards.values()
                if str(item.card_id) == first_card_id
            )
            assert TimerCode.DDS_WORK not in first_live.timers.timers
            assert (
                abs(
                    datetime.fromisoformat(first_row["received_at"]).timestamp()
                    - datetime.fromisoformat(second_row["received_at"]).timestamp()
                )
                < 1
            )
            first_service = snapshot["services"][0]
            station.send_json(
                {"type": "card.status", "service": first_service, "status": "accepted",
                 "comment": "Основание: доклад старшего.\nСведения: карточка принята."}
            )
            snapshot = read_until(station, "station.state")["snapshot"]
            first_elapsed = next(
                item
                for item in snapshot["queue_cards"]
                if item["card_id"] == first_card_id
            )["elapsed_ms"]

            time.sleep(0.03)
            # Card switching must publish its own fresh station snapshot; do
            # not let the one-second ticker mask a missing update.
            hub.stop_ticker(session_id)
            station.send_json({"type": "card.open", "card_id": second_card_id})
            second = read_until(station, "card.received")
            assert second["card"]["card_id"] == second_card_id
            second_live = next(
                item
                for item in state.desk.cards.values()
                if str(item.card_id) == second_card_id
            )
            assert TimerCode.DDS_WORK in second_live.timers.timers
            assert second_live.timers.timers[TimerCode.DDS_WORK].started_at is not None
            assert second["card"]["incident_type"] == "medical"
            assert (second["card_index"], second["card_total"]) == (2, 2)
            snapshot = read_until(station, "station.state")["snapshot"]
            second_queue_row = next(
                item
                for item in snapshot["queue_cards"]
                if item["card_id"] == second_card_id
            )
            assert second_queue_row["active"] is True
            assert second_queue_row["elapsed_ms"] >= first_elapsed
            first_queue_row = next(
                item
                for item in snapshot["queue_cards"]
                if item["card_id"] == first_card_id
            )
            assert first_queue_row["service_status"] == "accepted"
            assert first_queue_row["timer_stopped"] is True

            second_service = snapshot["services"][0]
            station.send_json(
                {"type": "card.status", "service": second_service, "status": "accepted",
                 "comment": "Основание: доклад старшего.\nСведения: карточка принята."}
            )
            read_until(station, "station.state")
            station.send_json({"type": "card.open", "card_id": first_card_id})
            assert (
                read_until(station, "card.received")["card"]["card_id"] == first_card_id
            )
            restored = read_until(station, "station.state")["snapshot"]
            assert restored["statuses"][first_service] == "accepted"
            station.send_json({"type": "card.next", "card_id": first_card_id})
            assert (
                read_until(station, "card.received")["card"]["card_id"]
                == second_card_id
            )
            after_close = read_until(station, "station.state")["snapshot"]
            assert len(after_close["queue_cards"]) == 1
            assert after_close["statuses"][second_service] == "accepted"
            station.send_json({"type": "card.next", "card_id": second_card_id})
            read_until(station, "score.ready")

        state = hub.get(session_id)
        assert state.ended and len(state.desk.completed) == 2
        assert len(state.score["card_results"]) == 2
        assert state.score["card_results"][0]["scenario_id"] == "fire-apartment-l2"
        assert state.score["card_results"][1]["scenario_id"] == "t20-2-stroke"
        assert "dds_primary" in {item["key"] for item in state.score["metrics"]}
        assert not any(
            item["key"] == "dds_grammar"
            for item in state.score["metrics"]
        )
        assert not any(item["code"] == "E3" for item in state.score["findings"]), (
            "обе карточки получили первую запись сразу"
        )
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
    control.send_json(
        {
            "type": "scenario.start",
            "scenario_id": "fire-apartment-l2",
            "scenario_ids": ["fire-apartment-l2", "t20-2-stroke"],
            "trainee": "Иванов",
            "mode": "training",
            "exercise": "dds",
        }
    )
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
            "fire-apartment-l2",
            "t20-2-stroke",
        ]
    finally:
        control_ctx.__exit__(None, None, None)


def test_each_dds_card_uses_its_own_scenario_weights():
    from pathlib import Path

    from app.domain.events import Exercise, SessionMode
    from app.scenarios.loader import load_file
    from app.session.dds import prepare_queue
    from app.session.finish import score_dds_card
    from app.session.state import SessionState

    root = Path(__file__).resolve().parents[2] / "scenarios"
    base = load_file(root / "fire-apartment-l2.yaml", root)
    first = base.model_copy(deep=True)
    second = base.model_copy(deep=True)
    first.score_weights = {"dds_primary": 7.0}
    second.score_weights = {"dds_primary": 2.0}
    state = SessionState(
        session_id=uuid4(),
        scenario_id=base.id,
        scenario_title=base.title,
        level=base.level.value,
        mode=SessionMode.TRAINING,
        exercise=Exercise.DDS,
    )
    prepare_queue(state, [first, second])
    first_card, second_card = state.desk.ordered()
    first_record = score_dds_card(state, first_card)
    second_record = score_dds_card(state, second_card)
    assert (
        next(item.weight for item in first_record.metrics if item.key == "dds_primary")
        == 7.0
    )
    assert (
        next(item.weight for item in second_record.metrics if item.key == "dds_primary")
        == 2.0
    )


def test_dds_station_command_and_finish_are_one_store_commit_each(client):
    """Команда пульта ДДС и её подтверждение фиксируются одним commit."""
    from app.session.store import LessonEnded, ScoreCalculated

    session_id, control = start(client)
    store = hub.store
    try:
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "card.received")
            service = read_until(station, "station.state")["snapshot"]["services"][0]
            before = len(store.commits)
            command_id = str(uuid4())
            station.send_json({
                "type": "card.status", "service": service, "status": "accepted",
                "comment": "Основание: профиль полномочий ДДС.\nСведения: карточка принята.",
                "_command_id": command_id,
            })
            read_until(station, "command.ack")
            assert len(store.commits) == before + 1
            assert command_id in store.snapshot(session_id)["processed_station_commands"]

            station.send_json({"type": "station.finish"})
            read_until(station, "score.ready")
            assert len(store.commits) == before + 2
            assert [type(item) for item in store.commits[-1][1]] == [LessonEnded, ScoreCalculated]
    finally:
        control.__exit__(None, None, None)
