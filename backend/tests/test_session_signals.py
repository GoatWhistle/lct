"""Колонка сигналов реестра: очередь, повторные отказы, курсант не на связи."""

import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.http import sessions as sessions_http
from app.config import get_settings
from app.main import app
from app.session.hub import hub
from app.session.store import MemorySessionStore

POOL = ["fire-apartment-l2", "t01-1-fire-container"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    get_settings.cache_clear()

    async def audit_override(*_args, **_kwargs):
        return None

    async def optional_session_override():
        yield None

    monkeypatch.setattr(sessions_http, "audit_required", audit_override)
    monkeypatch.setitem(
        app.dependency_overrides, sessions_http.optional_session, optional_session_override
    )
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


def start_two_card_dds(client):
    session_id = uuid4()
    context = client.websocket_connect(f"/ws/control/{session_id}")
    control = context.__enter__()
    control.send_json({
        "type": "scenario.start",
        "scenario_id": POOL[0],
        "trainee": "Иванов",
        "mode": "training",
        "exercise": "dds",
        "random_scenario_ids": POOL,
    })
    wait_for(lambda: hub.get(session_id))
    return session_id, control


def row_for(client, session_id):
    rows = client.get("/api/sessions/active").json()
    return next(item for item in rows if item["session_id"] == str(session_id))


def signal_kinds(row):
    return {signal["kind"] for signal in row["signals"]}


def test_signal_backlog_when_queue_reaches_threshold(client, monkeypatch):
    monkeypatch.setenv("SIGNAL_BACKLOG_THRESHOLD", "2")
    get_settings.cache_clear()
    session_id, control = start_two_card_dds(client)
    try:
        state = hub.get(session_id)
        assert len(state.desk.cards) == 2, "оба билета должны прийти сразу без интервала"
        row = row_for(client, session_id)
        assert row["dds_open_cards"] == 2
        assert "backlog" in signal_kinds(row)
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_signal_refusals_after_two_consecutive_declines(client):
    session_id, control = start_two_card_dds(client)
    try:
        state = hub.get(session_id)
        first_card, second_card = state.desk.ordered()
        with client.websocket_connect(f"/ws/station/{session_id}?role=dds") as station:
            read_until(station, "station.state")
            first_service = state.card_services(first_card)[0]
            station.send_json({
                "type": "card.status", "service": first_service, "status": "declined",
                "comment": "не наш адрес, передано в УК",
            })
            read_until(station, "station.state")

            row = row_for(client, session_id)
            assert "refusals" not in signal_kinds(row), "одного отказа недостаточно для сигнала"

            station.send_json({"type": "card.open", "card_id": str(second_card.card_id)})
            read_until(station, "station.state")
            second_service = state.card_services(second_card)[0]
            station.send_json({
                "type": "card.status", "service": second_service, "status": "declined",
                "comment": "не наша территория, передано в ОМВД",
            })
            read_until(station, "station.state")

        row = row_for(client, session_id)
        assert state.consecutive_refusals == 2
        assert "refusals" in signal_kinds(row)
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_signal_offline_when_station_socket_is_closed_past_the_window(client):
    session_id, control = start_two_card_dds(client)
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}?role=dds") as station:
            read_until(station, "station.state")
            row = row_for(client, session_id)
            assert "offline" not in signal_kinds(row), "сокет подключён — сигнала быть не должно"

        window = get_settings().signal_offline_window_seconds
        state.socket_last_seen_at = datetime.now(UTC) - timedelta(seconds=window + 1)
        row = row_for(client, session_id)
        assert "offline" in signal_kinds(row)
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)
