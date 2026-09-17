"""Профиль курсанта: история попыток и дельта между ними.

Тест сквозной — занятия проводятся через сокеты с включённым журналом,
профиль читается по HTTP. Без Postgres пропускается.
"""

import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from app.config import get_settings
from app.main import app
from app.session.hub import hub


@pytest.fixture(scope="module")
def client():
    # Доступность базы проверяется подключением к порту: синхронного драйвера
    # в проекте нет, и попытка проверить им даёт ложный пропуск теста.
    import socket
    from urllib.parse import urlparse

    url = urlparse(get_settings().database_url)
    try:
        socket.create_connection((url.hostname or "localhost", url.port or 5432), timeout=2).close()
    except OSError as exc:
        pytest.skip(f"Postgres недоступен ({exc}) — подними `make dev`")
    with TestClient(app) as test_client:
        yield test_client


def wait_for(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("не дождались")


def run_lesson(client, trainee: str, address: str | None) -> None:
    """Провести занятие: снять трубку, заполнить карточку, завершить, сдать самооценку."""
    session_id = uuid4()
    with client.websocket_connect(f"/ws/control/{session_id}") as control:
        control.send_json({
            "type": "scenario.start", "scenario_id": "fire-apartment-l2",
            "trainee": trainee, "mode": "training",
        })
        wait_for(lambda: hub.get(session_id))
        state = hub.get(session_id)
        with client.websocket_connect(f"/ws/call/{session_id}") as call:
            call.send_json({"type": "call.answer"})
            if address:
                call.send_json({"type": "kio.patch", "fields": {"address": address, "dds": "01"}})
                if state.slots:
                    state.slots.hear("Назовите адрес")
            call.send_json({"type": "dds.dispatch", "service": "01"})
            call.send_json({"type": "call.hangup"})
            wait_for(lambda: state.score is not None)
            call.send_json({"type": "self_assessment.submit", "missed": [], "comment": ""})
            wait_for(lambda: state.self_assessed)
    time.sleep(0.3)  # журнал пишет асинхронно


def test_profile_shows_attempts_and_delta(client):
    name = f"Курсант-{uuid4().hex[:6]}"
    run_lesson(client, name, address=None)           # первая попытка — ничего не добыл
    run_lesson(client, name, address="улица Ленина, 14")  # вторая — спросил адрес

    listing = client.get("/api/trainees").json()
    trainee = next(item for item in listing if item["name"] == name)

    profile = client.get(f"/api/trainees/{trainee['id']}/profile").json()
    assert [item["attempt"] for item in profile["attempts"]] == [1, 2], profile["attempts"]
    assert all(item["score"] is not None for item in profile["attempts"]), "оценка попытки не записана"

    assert profile["competencies"], "радар пуст"
    assert profile["deltas"], "дельта между попытками не посчитана"
    delta = profile["deltas"][0]
    assert delta["from_attempt"] == 1 and delta["to_attempt"] == 2
    if delta["facts_got"] is not None:
        assert delta["facts_got"] >= 0, "во второй попытке фактов добыто не меньше"


def test_profile_of_unknown_trainee_is_404(client):
    assert client.get(f"/api/trainees/{uuid4()}/profile").status_code == 404
