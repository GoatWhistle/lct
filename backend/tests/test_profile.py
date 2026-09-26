"""Профиль курсанта: история попыток и дельта между ними.

Тест сквозной — занятия проводятся через сокеты с включённым журналом,
профиль читается по HTTP. Без Postgres пропускается.
"""

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from app.config import get_settings
from app.api.auth import Principal
from app.api.http import trainees as trainees_api
from app.domain.roles import Role
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
        pytest.skip(f"Postgres недоступен ({exc}) — запусти `make test-db`")
    with TestClient(app) as test_client:
        # Сокеты закрыты ролями (lct-23): тесты входят так же,
        # как `make lesson`, — через dev-token за флагом.
        test_client.post("/api/auth/dev-token")
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

    # Личный совет должен отражать последнюю оценённую попытку, а не копить
    # нарушения за всю историю и не раскрывать неизвестные коды таксономии.
    latest_scored = next(item for item in reversed(profile["attempts"]) if item["score"] is not None)
    latest_codes = latest_scored["codes"]
    from app.scoring.group import RECOMMENDATIONS

    recommendations = profile["recommendations"]
    assert len(recommendations) <= 5
    expected_codes = {
        code for code, count in latest_codes.items()
        if code in RECOMMENDATIONS and isinstance(count, int) and count > 0
    }
    assert {item["code"] for item in recommendations} == expected_codes
    for item in recommendations:
        assert item["occurrences"] == latest_codes[item["code"]]
        assert item["title"] and item["recommendation"]


def test_profile_of_unknown_trainee_is_404(client):
    assert client.get(f"/api/trainees/{uuid4()}/profile").status_code == 404


def test_personal_recommendations_are_explainable_and_limited_to_known_codes():
    from app.api.http.trainees import _personal_recommendations

    result = _personal_recommendations({"D6": 1, "E1": 3, "unknown": 99, "D2": 0})
    assert [item.code for item in result] == ["E1", "D6"]
    assert result[0].title == "Пропущенный факт"
    assert result[0].recommendation
    assert result[0].occurrences == 3


def test_profile_read_is_audited_without_copying_profile_data(monkeypatch):
    trainee_id = uuid4()
    trainee = SimpleNamespace(id=trainee_id, name="Курсант Петров", group_id=None)
    who = Principal(
        login="trainee-login", full_name=trainee.name, role=Role.TRAINEE,
        trainee_id=trainee_id,
    )

    class Rows:
        def __iter__(self):
            return iter(())

    class Database:
        async def get(self, model, key):
            assert key == trainee_id
            return trainee

        async def execute(self, _statement):
            return Rows()

    events = []

    async def capture(actor, role, action, object_id=None, detail=""):
        events.append((actor, role, action, object_id, detail))

    monkeypatch.setattr(trainees_api, "require", lambda *_args, **_kwargs: who)
    monkeypatch.setattr(trainees_api, "audit_required", capture)
    result = asyncio.run(trainees_api.profile(trainee_id, object(), Database()))

    assert result.trainee.name == "Курсант Петров"
    assert events == [("trainee-login", "trainee", "trainee.profile.read", str(trainee_id), "")]


def test_certificate_export_is_audited_before_response(monkeypatch):
    trainee_id = uuid4()
    trainee = SimpleNamespace(id=trainee_id, name="Курсант Петров", group_id=None)
    who = Principal(login="teacher", full_name="Преподаватель", role=Role.INSTRUCTOR)

    class Result:
        def one(self):
            return 1, 90.0, datetime(2026, 9, 26, tzinfo=timezone.utc)

    class Database:
        async def scalar(self, _statement):
            return uuid4()

        async def get(self, model, key):
            assert key == trainee_id
            return trainee

        async def execute(self, _statement):
            return Result()

    events = []

    async def capture(actor, role, action, object_id=None, detail=""):
        events.append((actor, role, action, object_id, detail))

    monkeypatch.setattr(trainees_api, "require", lambda *_args, **_kwargs: who)
    monkeypatch.setattr(trainees_api, "audit_required", capture)
    monkeypatch.setattr(trainees_api, "certificate_pdf", lambda **_kwargs: b"%PDF-test")
    response = asyncio.run(trainees_api.certificate(trainee_id, object(), Database()))

    assert response.body == b"%PDF-test"
    assert events == [
        ("teacher", "instructor", "trainee.certificate.export.pdf", str(trainee_id), "")
    ]
