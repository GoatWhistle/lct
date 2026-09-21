"""Знание URL занятия не даёт курсанту доступ к чужому АРМ."""

import importlib
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.auth import Principal
from app.domain.roles import Role
from app.main import app
from app.session.hub import hub


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        test_client.post("/api/auth/dev-token")
        hub.journal = None
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
