"""Цепочка 112 → ДДС: карточка уходит снимком, диспетчер подтверждает или отбивает."""

import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.session.hub import hub


@pytest.fixture
def client():
    with TestClient(app) as test_client:
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
                station.send_json({"type": "card.ack"})
                measured = wait_for(lambda: state.timers.measured_ms(TimerCode.DDS_ACK) is not None)
        assert measured
    finally:
        control.__exit__(None, None, None)


def test_bounced_card_becomes_e6_with_the_reason(client):
    """Неполнота КИО перестаёт быть процентом в отчёте и становится
    сорванным выездом с конкретной причиной."""
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
                wait_for(lambda: state.bounced_fields)
                trainee.send_json({"type": "call.hangup"})
                wait_for(lambda: state.score is not None)

        response = client.get(f"/api/sessions/{session_id}/report").json()
        chain = [finding for finding in response["findings"] if finding["code"] == "E6"]
        assert chain, "возврат карточки должен попасть в разбор"
        assert "floor" in chain[0]["summary"] and "выезд сорван" in chain[0]["summary"]
    finally:
        control.__exit__(None, None, None)
