"""Знание URL занятия не даёт курсанту доступ к чужому АРМ."""

import asyncio
import importlib
import json
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.auth import Principal
from app.domain.roles import Role
from app.main import app
from app.scenarios import store
from app.session.hub import hub
from app.session.store import MemorySessionStore
from app.session.state import SessionState


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        test_client.post("/api/auth/dev-token")
        hub.store = MemorySessionStore()
        yield test_client


def test_trainee_cannot_open_foreign_or_unassigned_call_or_station(client, monkeypatch):
    session_id = uuid4()
    with client.websocket_connect(f"/ws/control/{session_id}") as control:
        control.send_json({"type": "scenario.start", "scenario_id": "fire-apartment-l2",
                           "trainee": "Назначенный", "mode": "training"})
        deadline = time.monotonic() + 3
        while hub.get(session_id) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        state = hub.get(session_id)
        assert state is not None

        owner_id, foreign_id = uuid4(), uuid4()
        modules = {
            "call": importlib.import_module("app.api.ws.call"),
            "station": importlib.import_module("app.api.ws.station"),
        }
        for assigned in (owner_id, None):
            state.trainee_id = assigned
            for role_name, module in modules.items():
                who = Principal(login="foreign", full_name="Чужой",
                                role=Role.TRAINEE, trainee_id=foreign_id)
                monkeypatch.setattr(module, "principal_of", lambda _ws, user=who: user)
                with client.websocket_connect(f"/ws/{role_name}/{session_id}") as socket:
                    event = socket.receive_json()
                    assert event["type"] == "error" and event["code"] == "forbidden"

        state.trainee_id = owner_id
        for role_name, module in modules.items():
            who = Principal(login="owner", full_name="Назначенный",
                            role=Role.TRAINEE, trainee_id=owner_id)
            monkeypatch.setattr(module, "principal_of", lambda _ws, user=who: user)
            with client.websocket_connect(f"/ws/{role_name}/{session_id}") as socket:
                socket.send_json({"type": "unknown"})
                for _ in range(8):
                    event = socket.receive_json()
                    if event["type"] == "error":
                        assert event["code"] == "unsupported_event"
                        break
                else:
                    raise AssertionError("владелец занятия не допущен")


def test_instructor_cannot_start_or_queue_another_instructors_scenario(client, monkeypatch):
    source = store.get("fire-apartment-l2")
    assert source is not None
    private = source.model_copy(update={"id": "private-ws-owner-test"}, deep=True)
    store.register_owned_scenario(private, "another-instructor")
    emitted = []
    monkeypatch.setattr(hub, "to_observers", lambda _session_id, event: emitted.append(event))
    public_id = "fire-apartment-l2"
    cases = [
        {"scenario_id": private.id},
        {"scenario_id": public_id, "exercise": "dds",
         "scenario_ids": [public_id, private.id]},
        {"scenario_id": public_id, "exercise": "dds",
         "random_scenario_ids": [public_id, private.id]},
    ]
    sessions = []
    try:
        for fields in cases:
            session_id = uuid4()
            sessions.append(session_id)
            with client.websocket_connect(f"/ws/control/{session_id}") as control:
                control.send_json({
                    "type": "scenario.start", "trainee": "Курсант", "mode": "training",
                    **fields,
                })
                deadline = time.monotonic() + 3
                while len(emitted) < len(sessions) and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert emitted[-1].code.value == "scenario_invalid"
                assert hub.get(session_id) is None
    finally:
        for session_id in sessions:
            hub.drop(session_id)
        store._library.pop(private.id, None)
        store._demo_scenario_owners.pop(private.id, None)


def test_idle_control_socket_closes_when_backend_lease_becomes_uncertain(monkeypatch):
    control_module = importlib.import_module("app.api.ws.control")
    session_id = uuid4()
    state = SessionState(
        session_id=session_id, scenario_id="test", scenario_title="Тест", level="L1",
        mode="training",
    )
    hub.register(state)
    who = Principal(login="lease-owner", full_name="Преподаватель", role=Role.INSTRUCTOR)
    monkeypatch.setattr(control_module, "websocket_origin_allowed", lambda _ws: True)
    monkeypatch.setattr(control_module, "principal_of", lambda _ws: who)

    class IdleSocket:
        accepted = False
        closed = None
        sent = []

        async def accept(self):
            self.accepted = True

        async def receive_json(self):
            await asyncio.sleep(0.02)
            await hub.fence(state)
            await asyncio.sleep(5)

        async def send_text(self, message):
            self.sent.append(json.loads(message))

        async def close(self, code=None):
            self.closed = code

    socket = IdleSocket()
    try:
        asyncio.run(control_module.control(socket, session_id))
        assert socket.accepted
        assert socket.closed == 1012
        assert socket.sent[-1]["message"].endswith("переподключитесь.")
    finally:
        hub.drop(session_id)


def test_control_command_checkpoint_failure_returns_fencing_error(monkeypatch):
    control_module = importlib.import_module("app.api.ws.control")
    session_id = uuid4()
    state = SessionState(
        session_id=session_id, scenario_id="test", scenario_title="Тест", level="L1",
        mode="training", owner_login="lease-owner",
    )

    class BrokenStore(MemorySessionStore):
        async def commit(self, _state, _records=()):
            raise OSError("simulated database partition")

    class OneCommandSocket:
        accepted = False
        closed = None
        sent = []

        async def accept(self):
            self.accepted = True

        async def receive_json(self):
            return {"type": "reference.play"}

        async def send_text(self, message):
            self.sent.append(json.loads(message))

        async def close(self, code=None):
            self.closed = code

    who = Principal(login="lease-owner", full_name="Преподаватель", role=Role.INSTRUCTOR)
    socket = OneCommandSocket()
    old_store = hub.store
    hub.store = BrokenStore()
    hub.register(state)
    monkeypatch.setattr(control_module, "websocket_origin_allowed", lambda _ws: True)
    monkeypatch.setattr(control_module, "principal_of", lambda _ws: who)
    try:
        with hub.observer(session_id) as observer_queue:
            asyncio.run(control_module.control(socket, session_id))
            assert socket.accepted
            assert state.lease_fenced
            assert socket.closed == 1012
            assert socket.sent[-1]["type"] == "error"
            assert "переподключитесь" in socket.sent[-1]["message"]
            event = observer_queue.get_nowait()
            assert "переподключитесь" in event.message
            assert observer_queue.empty(), "uncommitted controller event leaked to observers"
    finally:
        hub.store = old_store
        hub.drop(session_id)


def test_control_does_not_lose_command_read_during_fencing_poll(monkeypatch):
    """Опрос fencing по таймеру не отменяет чтение, уже забравшее кадр из сокета.

    Сокет отдаёт команду, но возвращается из receive позже тика опроса — так
    выглядит задержка планировщика под нагрузкой. Отмена чтения на тике
    выбрасывает уже прочитанную команду.
    """
    control_module = importlib.import_module("app.api.ws.control")
    monkeypatch.setattr(control_module, "_FENCE_POLL_SECONDS", 0.02)
    session_id = uuid4()
    state = SessionState(
        session_id=session_id, scenario_id="test", scenario_title="Тест", level="L1",
        mode="training", owner_login="lease-owner",
    )
    hub.register(state)
    who = Principal(login="lease-owner", full_name="Преподаватель", role=Role.INSTRUCTOR)
    monkeypatch.setattr(control_module, "websocket_origin_allowed", lambda _ws: True)
    monkeypatch.setattr(control_module, "principal_of", lambda _ws: who)

    class SlowReturnSocket:
        def __init__(self):
            self.wire = [{"type": "no.such.command"}]
            self.lost = []
            self.closed = None
            self.sent = []

        async def accept(self):
            pass

        async def receive_json(self):
            if not self.wire:
                await hub.fence(state)
                await asyncio.sleep(5)
            message = self.wire.pop(0)
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                self.lost.append(message)
                raise
            return message

        async def send_text(self, message):
            self.sent.append(json.loads(message))

        async def close(self, code=None):
            self.closed = code

    socket = SlowReturnSocket()
    try:
        with hub.observer(session_id) as observer_queue:
            asyncio.run(control_module.control(socket, session_id))
            assert socket.lost == []
            event = observer_queue.get_nowait()
            assert event.code == "unsupported_event"
            assert socket.closed == 1012
            assert socket.sent[-1]["message"].endswith("переподключитесь.")
    finally:
        hub.drop(session_id)
