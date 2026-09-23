"""Явный локальный демо-режим не зависит от Postgres."""

import time
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.auth import DEMO_TRAINEE_ID
from app.config import get_settings
from app.main import app
from app.session.hub import hub


def _wait_for(predicate, timeout=3):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("занятие не стартовало")


def _read_until(socket, wanted):
    for _ in range(20):
        event = socket.receive_json()
        if event["type"] == wanted:
            return event
    raise AssertionError(f"не пришло событие {wanted}")


def test_demo_without_db_starts_dds_and_issues_owned_trainee_cookie(monkeypatch):
    monkeypatch.setenv("DEMO_NO_DB", "true")
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    monkeypatch.setenv("VOICE_ENABLED", "false")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            health = client.get("/api/health").json()
            assert health["status"] == "ok" and health["demo_no_db"] is True
            assert health["scenarios_loaded"] > 0
            assert hub.journal is None

            assert client.post("/api/auth/login", json={
                "login": "demo-instructor", "password": "demo"
            }).json()["role"] == "instructor"
            assert client.post("/api/auth/login", json={
                "login": "unknown", "password": "demo"
            }).status_code == 401
            assert client.post("/api/auth/dev-token?role=trainee").json()["trainee_id"] == str(DEMO_TRAINEE_ID)
            assert client.post("/api/auth/dev-token").json()["role"] == "instructor"
            assert client.get("/api/trainees").json() == [
                {"id": str(DEMO_TRAINEE_ID), "name": "Демо-курсант", "group": None,
                 "service": "Служба 101"}
            ]
            # БД-зависимые экраны получают быстрый и явный отказ, не ждут TCP timeout.
            assert client.get("/api/sessions").json()["detail"] == "database_disabled_demo"

            session_id = uuid4()
            with client.websocket_connect(f"/ws/control/{session_id}") as control:
                control.send_json({
                    "type": "scenario.start", "scenario_id": "fire-apartment-l2",
                    "trainee": "Демо-курсант", "trainee_id": str(DEMO_TRAINEE_ID),
                    "mode": "training", "exercise": "dds",
                })
                state = _wait_for(lambda: hub.get(session_id))
                assert state.trainee_id == DEMO_TRAINEE_ID
                who = client.post("/api/auth/login", json={
                    "login": "demo-trainee", "password": "demo"
                }).json()
                assert who["trainee_id"] == str(DEMO_TRAINEE_ID)
                with client.websocket_connect(f"/ws/station/{session_id}") as station:
                    assert _read_until(station, "card.received")["card"]["address"]
                    assert _read_until(station, "station.state")["snapshot"]["card_index"] == 1
    finally:
        get_settings.cache_clear()
