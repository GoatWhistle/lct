"""Колонка сигналов реестра: очередь, повторные отказы, курсант не на связи."""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.auth import Principal
from app.api.http import sessions as sessions_http
from app.api.ws import session as session_ws
from app.config import get_settings
from app.domain.events import Exercise, SessionMode
from app.domain.roles import Role
from app.main import app
from app.session.checkpoint import dump_state, load_state
from app.session import hub as hub_module
from app.session.hub import SessionHub, hub
from app.session.state import SessionState
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
            before = set(hub._sessions)
            try:
                yield test_client
            finally:
                for session_id in set(hub._sessions) - before:
                    hub.drop(session_id)
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


def start_two_card_dds(client, scenario_ids=POOL):
    session_id = uuid4()
    context = client.websocket_connect(f"/ws/control/{session_id}")
    control = context.__enter__()
    control.send_json({
        "type": "scenario.start",
        "scenario_id": POOL[0],
        "trainee": "Иванов",
        "mode": "training",
        "exercise": "dds",
        "random_scenario_ids": scenario_ids,
    })
    wait_for(lambda: hub.get(session_id))
    return session_id, control


def start_card_handoff(client):
    session_id = uuid4()
    context = client.websocket_connect(f"/ws/control/{session_id}")
    control = context.__enter__()
    control.send_json({
        "type": "scenario.start", "scenario_id": POOL[0], "trainee": "Иванов",
        "mode": "training", "exercise": "card", "handoff_to_dds": True,
        "scenario_ids": POOL,
    })
    wait_for(lambda: hub.get(session_id))
    return session_id, control


def start_call(client):
    session_id = uuid4()
    context = client.websocket_connect(f"/ws/control/{session_id}")
    control = context.__enter__()
    control.send_json({
        "type": "scenario.start", "scenario_id": POOL[0], "trainee": "Иванов",
        "mode": "training", "exercise": "call",
    })
    wait_for(lambda: hub.get(session_id))
    return session_id, control


def row_for(client, session_id):
    rows = client.get("/api/sessions/active").json()
    return next(item for item in rows if item["session_id"] == str(session_id))


def signal_kinds(row):
    return {signal["kind"] for signal in row["signals"]}


def assigned_trainee(state, monkeypatch):
    state.trainee_id = uuid4()
    who = Principal(login="курсант", full_name="Курсант", role=Role.TRAINEE,
                    trainee_id=state.trainee_id)
    monkeypatch.setattr(session_ws, "principal_of", lambda _ws: who)


def registry_time(monkeypatch, since, seconds):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return since + timedelta(seconds=seconds)

    monkeypatch.setattr(sessions_http, "datetime", Clock)


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


def test_acceptance_clears_refusal_streak(client):
    session_id, control = start_two_card_dds(
        client, [*POOL, "t20-2-stroke"],
    )
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "station.state")
            for card in state.desk.ordered()[:2]:
                station.send_json({"type": "card.open", "card_id": str(card.card_id)})
                read_until(station, "station.state")
                station.send_json({
                    "type": "card.status", "service": state.card_services(card)[0],
                    "status": "declined", "comment": "Не наша территория, передано дежурному",
                })
                read_until(station, "station.state")
            assert state.consecutive_refusals == 2
            third = state.desk.ordered()[2]
            station.send_json({"type": "card.open", "card_id": str(third.card_id)})
            read_until(station, "station.state")
            station.send_json({
                "type": "card.status", "service": state.card_services(third)[0],
                "status": "accepted", "comment": "Карточка принята диспетчером",
            })
            read_until(station, "station.state")
            assert state.consecutive_refusals == 0
            assert "refusals" not in signal_kinds(row_for(client, session_id))
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_signal_offline_when_station_socket_is_closed_past_the_window(client, monkeypatch):
    session_id, control = start_two_card_dds(client)
    try:
        state = hub.get(session_id)
        assigned_trainee(state, monkeypatch)
        with client.websocket_connect(f"/ws/station/{session_id}?role=dds") as station:
            read_until(station, "station.state")
            assert hub.station_connected(session_id)
        assert not hub.station_connected(session_id)
        disconnected_at = state.socket_last_seen_at
        assert disconnected_at is not None
        window = get_settings().signal_offline_window_seconds
        registry_time(monkeypatch, disconnected_at, window - 1)
        assert "offline" not in signal_kinds(row_for(client, session_id))
        registry_time(monkeypatch, disconnected_at, window + 1)
        row = row_for(client, session_id)
        assert "offline" in signal_kinds(row)
        assert state.socket_last_seen_at == disconnected_at, "GET реестра не должен менять занятие"
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_signal_offline_without_first_connection(client, monkeypatch):
    session_id, control = start_two_card_dds(client)
    try:
        state = hub.get(session_id)
        started = state.socket_last_seen_at
        assert started is not None
        registry_time(monkeypatch, started, get_settings().signal_offline_window_seconds + 1)
        assert "offline" in signal_kinds(row_for(client, session_id))
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_call_socket_disconnection_triggers_offline(client, monkeypatch):
    session_id, control = start_call(client)
    try:
        state = hub.get(session_id)
        assigned_trainee(state, monkeypatch)
        with client.websocket_connect(f"/ws/call/{session_id}") as call:
            read_until(call, "call.incoming")
            assert hub.trainee_connected(session_id)
        assert not hub.trainee_connected(session_id)
        disconnected_at = state.socket_last_seen_at
        registry_time(monkeypatch, disconnected_at,
                      get_settings().signal_offline_window_seconds + 1)
        assert "offline" in signal_kinds(row_for(client, session_id))
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_teacher_socket_does_not_hide_offline(client, monkeypatch):
    session_id, control = start_two_card_dds(client)
    try:
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "station.state")
            assert not hub.station_connected(session_id)
            registry_time(monkeypatch, state.socket_last_seen_at,
                          get_settings().signal_offline_window_seconds + 1)
            assert "offline" in signal_kinds(row_for(client, session_id))
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_last_of_two_trainee_sockets_starts_offline_window(client, monkeypatch):
    session_id, control = start_two_card_dds(client)
    try:
        state = hub.get(session_id)
        assigned_trainee(state, monkeypatch)
        with client.websocket_connect(f"/ws/station/{session_id}") as first:
            read_until(first, "station.state")
            with client.websocket_connect(f"/ws/station/{session_id}") as second:
                read_until(second, "station.state")
            assert hub.station_connected(session_id)
            registry_time(monkeypatch, state.socket_last_seen_at,
                          get_settings().signal_offline_window_seconds + 1)
            assert "offline" not in signal_kinds(row_for(client, session_id))
        disconnected_at = state.socket_last_seen_at
        registry_time(monkeypatch, disconnected_at,
                      get_settings().signal_offline_window_seconds + 1)
        assert "offline" in signal_kinds(row_for(client, session_id))
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_offline_window_survives_checkpoint_restore(client, monkeypatch):
    session_id, control = start_two_card_dds(client)
    try:
        state = hub.get(session_id)
        assigned_trainee(state, monkeypatch)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "station.state")
        disconnected_at = state.socket_last_seen_at
        restored = load_state(dump_state(state), datetime.now(UTC))
        hub.drop(session_id)
        asyncio.run(hub._adopt(restored))
        assert restored.socket_last_seen_at == disconnected_at
        registry_time(monkeypatch, disconnected_at,
                      get_settings().signal_offline_window_seconds + 1)
        assert "offline" in signal_kinds(row_for(client, session_id))
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_takeover_starts_window_when_socket_was_open(client, monkeypatch):
    session_id, control = start_two_card_dds(client)
    try:
        state = hub.get(session_id)
        assigned_trainee(state, monkeypatch)
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "station.state")
            assert state.socket_connected_at_checkpoint
            snapshot = dump_state(state)
        old_time = state.socket_last_seen_at
        takeover_at = old_time + timedelta(minutes=10)
        monkeypatch.setattr(hub_module, "now_utc", lambda: takeover_at)
        restored = load_state(snapshot, datetime.now(UTC))

        class CapturingStore(MemorySessionStore):
            def __init__(self):
                self.saved = None

            async def commit(self, state, _records=()):
                self.saved = dump_state(state)

        store = CapturingStore()
        local_hub = SessionHub(store)
        try:
            assert asyncio.run(local_hub._adopt(restored))
            assert restored.socket_last_seen_at == takeover_at
            assert not restored.socket_connected_at_checkpoint
            assert datetime.fromisoformat(
                store.saved["socket_last_seen_at"].replace("Z", "+00:00")
            ) == takeover_at
            assert store.saved["socket_connected_at_checkpoint"] is False
        finally:
            local_hub.stop_ticker(session_id)
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_card_handoff_uses_trainee_station_presence(client, monkeypatch):
    session_id, control = start_card_handoff(client)
    try:
        state = hub.get(session_id)
        assigned_trainee(state, monkeypatch)
        with monkeypatch.context() as clock:
            with client.websocket_connect(f"/ws/call/{session_id}") as call:
                read_until(call, "card.briefing")
                call.send_json({"type": "card.submit"})
                read_until(call, "call.ended")
                handoff_at = state.socket_last_seen_at
                clock.setattr(hub_module, "now_utc", lambda: handoff_at + timedelta(minutes=1))
        assert state.socket_last_seen_at == handoff_at, "старый call-сокет не сбрасывает окно ДДС"
        assert state.exercise.value == "card" and state.dds_phase
        assert state.desk.scenarios
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            read_until(station, "station.state")
            registry_time(monkeypatch, state.socket_last_seen_at,
                          get_settings().signal_offline_window_seconds + 1)
            row = row_for(client, session_id)
            assert row["dds_open_cards"] > 0
            assert "offline" not in signal_kinds(row)
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_checkpoint_on_another_node_marks_presence_unknown(client, monkeypatch):
    session_id, control = start_two_card_dds(client)
    try:
        state = hub.get(session_id)
        snapshot = dump_state(state)
        checkpoint_at = datetime.now(UTC)
        hub.drop(session_id)

        class CheckpointDB:
            async def scalars(self, _query):
                row = SimpleNamespace(id=session_id, owner_login="dev",
                                      live_state=snapshot, checkpoint_at=checkpoint_at)
                return SimpleNamespace(all=lambda: [row])

        monkeypatch.setattr(sessions_http, "require", lambda _request, _role:
                            Principal(login="dev", full_name="Преподаватель",
                                      role=Role.INSTRUCTOR))
        rows = asyncio.run(sessions_http.active(None, db=CheckpointDB()))
        row = next(item for item in rows if item.session_id == session_id)
        assert row.presence_known is False
        assert "offline" not in {item.kind for item in row.signals}
    finally:
        hub.stop_ticker(session_id)
        control.__exit__(None, None, None)


def test_failed_presence_commit_does_not_leave_connected_socket():
    class BrokenStore(MemorySessionStore):
        async def commit(self, _state, _records=()):
            raise OSError("test commit failure")

    local_hub = SessionHub(BrokenStore())
    state = local_hub.register(SessionState(
        session_id=uuid4(), scenario_id="test", scenario_title="Тест", level="L1",
        mode=SessionMode.TRAINING, exercise=Exercise.DDS,
    ))

    async def connect():
        async with local_hub.trainee_socket(state.session_id, station=True, trainee=True):
            assert local_hub.is_lease_fenced(state.session_id)

    asyncio.run(connect())
    assert not local_hub.station_connected(state.session_id)


def test_failed_presence_commit_closes_station_with_fencing_code(client, monkeypatch):
    class BrokenStore(MemorySessionStore):
        async def commit(self, _state, _records=()):
            raise OSError("test commit failure")

    session_id, control = start_two_card_dds(client)
    try:
        assigned_trainee(hub.get(session_id), monkeypatch)
        hub.store = BrokenStore()
        with client.websocket_connect(f"/ws/station/{session_id}") as station:
            event = station.receive_json()
            assert event["type"] == "error" and event["code"] == "internal"
            with pytest.raises(WebSocketDisconnect) as closed:
                station.receive_json()
            assert closed.value.code == 1012
        assert not hub.station_connected(session_id)
    finally:
        control.__exit__(None, None, None)
