"""Цепочка 112 → ДДС: карточка уходит снимком, диспетчер подтверждает или отбивает."""

import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.http import sessions as sessions_http
from app.main import app
from app.session.hub import hub


@pytest.fixture
def client(monkeypatch):
    async def audit_in_memory(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sessions_http, "audit_required", audit_in_memory)
    with TestClient(app) as test_client:
        # Сокеты закрыты ролями (lct-23): тесты входят так же,
        # как `make lesson`, — через dev-token за флагом.
        test_client.post("/api/auth/dev-token")
        hub.journal = None
        yield test_client


def wait_for(predicate, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("не дождались")


def read_until(ws, event_type: str, limit: int = 20) -> dict:
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == event_type:
            return message
    raise AssertionError(f"событие {event_type} не пришло")


def lesson(client):
    session_id = uuid4()
    control = client.websocket_connect(f"/ws/control/{session_id}")
    socket = control.__enter__()
    socket.send_json({"type": "scenario.start", "scenario_id": "fire-apartment-l2",
                      "trainee": "Иванов", "mode": "training"})
    wait_for(lambda: hub.get(session_id))
    return session_id, control, socket


def test_card_goes_to_the_dispatcher_frozen(client):
    """Снимок не меняется после передачи: оператор не дописывает задним числом."""
    session_id, control, _ = lesson(client)
    try:
        with client.websocket_connect(f"/ws/station/{session_id}?role=dds_01") as station:
            with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
                trainee.send_json({"type": "call.answer"})
                trainee.send_json({"type": "kio.patch", "fields": {"address": "улица Ленина, 14", "dds": "01"}})
                wait_for(lambda: hub.get(session_id).kio.address)
                trainee.send_json({"type": "dds.dispatch", "service": "01"})
                received = read_until(station, "card.received")

                # Правка после передачи в снимок не попадает.
                trainee.send_json({"type": "kio.patch", "fields": {"floor": "5"}})
                wait_for(lambda: hub.get(session_id).kio.floor == "5")

        assert received["card"]["address"] == "улица Ленина, 14"
        assert received["card"]["floor"] is None, "снимок изменился после передачи"
        assert received["from_operator"] == "Иванов"
    finally:
        control.__exit__(None, None, None)


def test_station_joining_late_still_gets_the_card(client):
    """Диспетчер садится за АРМ, когда вызов уже идёт."""
    session_id, control, _ = lesson(client)
    try:
        with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
            trainee.send_json({"type": "call.answer"})
            trainee.send_json({"type": "dds.dispatch", "service": "01"})
            wait_for(lambda: hub.get(session_id).dispatched_card)

        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            assert read_until(station, "card.received")["card"] is not None
    finally:
        control.__exit__(None, None, None)


def test_acknowledgement_stops_the_four_second_norm(client):
    from app.domain.timers import TimerCode

    session_id, control, _ = lesson(client)
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
                trainee.send_json({"type": "call.answer"})
                trainee.send_json({"type": "dds.dispatch", "service": "01"})
                read_until(station, "card.received")
                station.send_json({"type": "card.ack", "comment": "Подтверждение приёма зафиксировано по докладу старшего группы."})
                measured = wait_for(lambda: state.timers.measured_ms(TimerCode.DDS_ACK) is not None)
        assert measured
    finally:
        control.__exit__(None, None, None)


def test_dds_cannot_return_card_for_112_quality_control(client):
    """ДДС ставит статусы своей службы, а качество КИО контролирует 112."""
    session_id, control, _ = lesson(client)
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            with client.websocket_connect(f"/ws/call/{session_id}") as trainee:
                trainee.send_json({"type": "call.answer"})
                trainee.send_json({"type": "dds.dispatch", "service": "01"})
                read_until(station, "card.received")
                station.send_json({"type": "card.bounce", "missing_fields": ["floor", "victims_count"],
                                   "comment": "куда ехать без этажа"})
                error = read_until(station, "error")
                assert "служба контроля 112" in error["message"]
                assert state.bounced_fields == []
                trainee.send_json({"type": "call.hangup"})
                wait_for(lambda: state.score is not None)

        response = client.get(f"/api/sessions/{session_id}/report").json()
        chain = [finding for finding in response["findings"] if finding["code"] == "E6"]
        assert chain == []
    finally:
        control.__exit__(None, None, None)


# ── статусы реагирования (lct-33) ──


def dispatched(client):
    """Занятие, доведённое до передачи карточки в ДДС, и подключённая станция."""
    session_id, control, _ = lesson(client)
    station_ctx = client.websocket_connect(f"/ws/station/{session_id}?role=dds_01")
    station = station_ctx.__enter__()
    call_ctx = client.websocket_connect(f"/ws/call/{session_id}")
    trainee = call_ctx.__enter__()
    trainee.send_json({"type": "call.answer"})
    trainee.send_json(
        {"type": "kio.patch", "fields": {"signs": ["жилой дом", "балкон", "открытое пламя"]}}
    )
    wait_for(lambda: hub.get(session_id).kio.incident_code)
    trainee.send_json({"type": "dds.dispatch", "service": "01"})
    read_until(station, "card.received")
    return session_id, station, (control, station_ctx, call_ctx)


def close_all(contexts):
    for context in contexts:
        context.__exit__(None, None, None)


def test_station_sees_the_notify_list_from_the_classifier(client):
    """Диспетчеру есть что отмечать только потому, что список оповещения
    посчитан по ЕКП, а не выбран оператором (lct-32)."""
    session_id, station, contexts = dispatched(client)
    try:
        state = read_until(station, "station.state")
        assert state["snapshot"]["services"] == ["Служба 101"]
        assert "Служба 101" in state["snapshot"]["recipient_services"]
        assert len(state["snapshot"]["recipient_services"]) > 1
        assert state["snapshot"]["statuses"]["Служба 101"] == "added"
    finally:
        close_all(contexts)


def test_dispatcher_sets_a_status_and_the_card_follows(client):
    session_id, station, contexts = dispatched(client)
    try:
        read_until(station, "station.state")
        station.send_json({"type": "card.status", "service": "Служба 101", "status": "accepted"})
        error = read_until(station, "error")
        assert "комментар" in error["message"]
        assert hub.get(session_id).status_log == []
        command_id = str(uuid4())
        accepted = {
            "type": "card.status", "service": "Служба 101", "status": "accepted",
            "comment": "Старший группы подтвердил приём карточки.",
            "_command_id": command_id,
        }
        station.send_json(accepted)
        state = read_until(station, "station.state")
        assert state["snapshot"]["statuses"]["Служба 101"] == "accepted"
        assert read_until(station, "command.ack")["command_id"] == command_id
        # Simulate a retry after the client lost the ACK: the committed ID
        # returns another ACK but does not append a second status action.
        station.send_json(accepted)
        assert read_until(station, "command.ack")["command_id"] == command_id
        runtime = hub.get(session_id)
        assert sum(item.service == "Служба 101" for item in runtime.status_log) == 1
        assert "responding" in state["snapshot"]["available"]["Служба 101"]
        assert "accepted" not in state["snapshot"]["available"]["Служба 101"]
    finally:
        close_all(contexts)


def test_out_of_order_status_is_rejected_with_a_reason(client):
    """Автомат живёт на сервере: фронт может ошибиться, состояние — нет."""
    session_id, station, contexts = dispatched(client)
    try:
        read_until(station, "station.state")
        station.send_json({"type": "card.status", "service": "Служба 101", "status": "arrived",
                           "comment": "Бригада доложила старшему о прибытии."})
        error = read_until(station, "error")
        assert "Принята" in error["message"]
        assert hub.get(session_id).status_log == []
    finally:
        close_all(contexts)


def test_refusal_without_a_comment_is_rejected(client):
    session_id, station, contexts = dispatched(client)
    try:
        read_until(station, "station.state")
        station.send_json({"type": "card.status", "service": "Служба 101", "status": "declined"})
        error = read_until(station, "error")
        assert "комментар" in error["message"]
    finally:
        close_all(contexts)


def test_ack_button_still_works_and_counts_as_accepted(client):
    """Кнопка подтверждения из живой цепочки (lct-20) осталась и означает
    «Принята» у главной службы — иначе два экрана учили бы разному."""
    session_id, station, contexts = dispatched(client)
    try:
        read_until(station, "station.state")
        station.send_json({"type": "card.ack", "comment": "Старший группы подтвердил приём карточки."})
        state = read_until(station, "station.state")
        assert state["snapshot"]["statuses"]["Служба 101"] == "accepted"
    finally:
        close_all(contexts)
