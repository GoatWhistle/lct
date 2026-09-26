from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api import auth
from app.api.http import scenario_submissions
from app.api.http.scenario_submissions import reset_demo_submissions
from app.config import get_settings
from app.db.models import AuditLog, Group, Scenario, ScenarioSubmission, Trainee
from app.domain import ekp
from app.main import app
from app.scenarios import store
from app.session.dds import build_card
from app.session.timers import SessionTimers


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DEMO_NO_DB", "true")
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    get_settings.cache_clear()
    store.reset_demo_drafts()
    reset_demo_submissions()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        get_settings.cache_clear()


def _login(client: TestClient, role: str) -> None:
    response = client.post("/api/auth/dev-token", params={"role": role})
    assert response.status_code == 200, response.text


def _student_kio(
    description="В мастерской виден дым из повреждённого оборудования.",
    address="Москва, учебная улица, дом 10",
):
    source = store.get("t01-1-fire-container")
    assert source is not None
    return {
        "caller_name": "Учебный заявитель",
        "caller_contact": "+7 900 000-00-00",
        "address": address,
        "description": description,
        "incident_group": ekp.incident(source.ground_truth.incident_code).group,
        "signs": source.signs,
        "incident_type": "fire",
        "dds": source.ground_truth.dds.value,
        "victims_count": 0,
        "fire": {"object_kind": "оборудование", "fire_nature": "задымление"},
    }


def test_trainee_scenario_catalog_and_detail_only_expose_safe_self_practice_fields(client):
    _login(client, "trainee")
    listed = client.get("/api/scenarios").json()
    item = next(row for row in listed if row["id"] == "t01-1-fire-container")
    assert set(item) == {"id", "title", "level", "modes"}
    assert "self" in item["modes"]
    detail = client.get("/api/scenarios/t01-1-fire-container")
    assert detail.status_code == 200
    assert set(detail.json()) == {"id", "title", "level", "modes"}
    assert client.get("/api/scenarios/t01-1-fire-container").json().get("ground_truth") is None


def test_student_submission_is_moderated_then_enters_dds_bank(client):
    _login(client, "trainee")
    created = client.post(
        "/api/scenario-submissions",
        json={
            "title": "Пожар в мастерской",
            "level": "L2",
            "kio": _student_kio(
                "В мастерской на первом этаже виден дым, люди вышли наружу.",
                "Москва, улица Примерная, дом 12",
            ),
        },
    )
    assert created.status_code == 201, created.text
    submission_id = created.json()["id"]
    assert created.json()["status"] == "pending"
    assert created.json()["scenario_id"] is None
    assert client.get("/api/scenarios").status_code == 200
    assert not any("student-created" in item.get("topics", [])
                   for item in client.get("/api/scenarios").json())

    # Курсанту разрешено видеть своё предложение, но не публиковать его.
    own = client.get("/api/scenario-submissions").json()
    assert [item["id"] for item in own] == [submission_id]
    denied = client.post(
        f"/api/scenario-submissions/{submission_id}/review",
        json={"decision": "approve"},
    )
    assert denied.status_code == 403

    client.post("/api/auth/logout")
    _login(client, "instructor")
    pending = client.get("/api/scenario-submissions").json()
    assert pending[0]["title"] == "Пожар в мастерской"
    approved = client.post(
        f"/api/scenario-submissions/{submission_id}/review",
        json={"decision": "approve", "comment": "Факты проверены."},
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "approved"
    assert body["scenario_id"].startswith("student-")

    scenario = store.get(body["scenario_id"])
    assert scenario is not None
    assert scenario.ground_truth.address == "Москва, улица Примерная, дом 12"
    assert scenario.ground_truth.victims == 0
    assert "student-created" in scenario.topics
    listed = client.get("/api/scenarios").json()
    published = next(item for item in listed if item["id"] == scenario.id)
    assert published["source"] == "trainee"
    assert published["outcome"] == "card"
    assert published["dds"] == "01"

    repeated = client.post(
        f"/api/scenario-submissions/{submission_id}/review",
        json={"decision": "approve"},
    )
    assert repeated.status_code == 409


def test_submitted_kio_is_kept_intact_and_becomes_the_dds_card_after_approval(client):
    source = store.get("t01-1-fire-container")
    assert source is not None
    _login(client, "trainee")
    created = client.post(
        "/api/scenario-submissions",
        json={
            "title": "КИО курсанта: контейнер во дворе",
            "level": "L2",
            "kio": {
                "caller_name": "Учебный заявитель",
                "caller_contact": "+7 900 000-00-00",
                "address": "Москва, учебная улица, дом 10",
                "description": "Во дворе открыто горит мусорный контейнер.",
                "incident_group": ekp.incident(source.ground_truth.incident_code).group,
                "signs": source.signs,
                "incident_type": "fire",
                "dds": source.ground_truth.dds.value,
                "victims_count": 0,
                "fire": {
                    "object_kind": "мусорный контейнер",
                    "fire_nature": "открытое пламя",
                },
            },
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "pending"
    assert body["kio"]["caller_number"] is None
    assert body["kio"]["caller_name"] == "Учебный заявитель"
    assert not any("student-created" in item.get("topics", [])
                   for item in client.get("/api/scenarios").json())

    client.post("/api/auth/logout")
    _login(client, "instructor")
    approved = client.post(
        f"/api/scenario-submissions/{body['id']}/review",
        json={"decision": "approve", "comment": "КИО проверена."},
    )
    assert approved.status_code == 200, approved.text
    scenario = store.get(approved.json()["scenario_id"])
    assert scenario is not None
    assert scenario.student_card is not None
    assert scenario.student_card.address == "Москва, учебная улица, дом 10"
    assert scenario.student_card.caller_name == "Учебный заявитель"
    assert scenario.student_card.victims_count == 0
    assert scenario.student_card.fire.object_kind == "мусорный контейнер"
    assert scenario.student_card.signs == source.signs
    assert "moderated-kio" in scenario.topics
    card = build_card(scenario, 0, SessionTimers().limits)
    assert card.dispatched_card.address == scenario.student_card.address
    assert card.dispatched_card.caller_name == "Учебный заявитель"
    assert card.dispatched_card.fire.object_kind == "мусорный контейнер"
    assert any(
        item["id"] == scenario.id and item["source"] == "trainee"
        for item in client.get("/api/scenarios").json()
    )


def test_database_submission_path_persists_kio_and_publishes_on_approval(
    client,
    monkeypatch,
):
    source = store.get("t01-1-fire-container")
    assert source is not None
    trainee_id = UUID("00000000-0000-4000-8000-000000000112")
    group_id = UUID("00000000-0000-4000-8000-000000000113")

    class FakeDb:
        submission = None
        scenario_row = None

        def __init__(self):
            self.audit_rows = []
            self.commit_rows = []

        async def get(self, model, _key):
            if model is Trainee:
                return SimpleNamespace(id=trainee_id, group_id=group_id)
            if model is Group:
                return SimpleNamespace(id=group_id, owner_login="demo-instructor")
            raise AssertionError(f"unexpected model: {model}")

        def add(self, row):
            if isinstance(row, ScenarioSubmission):
                self.submission = row
                row.status = "pending"
                row.created_at = datetime.now(UTC)
            elif isinstance(row, Scenario):
                self.scenario_row = row
            elif isinstance(row, AuditLog):
                self.audit_rows.append(row)
            else:
                raise AssertionError(f"unexpected row: {type(row)}")

        async def commit(self):
            self.commit_rows.append(tuple(self.audit_rows))

        async def scalar(self, _query):
            return self.submission

    fake_db = FakeDb()

    async def session_override():
        yield fake_db

    monkeypatch.setitem(
        app.dependency_overrides,
        scenario_submissions.submission_session,
        session_override,
    )
    _login(client, "trainee")
    created = client.post(
        "/api/scenario-submissions",
        json={
            "title": "КИО в DB-пути",
            "level": "L2",
            "kio": {
                "caller_name": "Учебный заявитель",
                "address": "Москва, тестовая улица, дом 3",
                "description": "Во дворе открыто горит мусорный контейнер.",
                "incident_group": ekp.incident(source.ground_truth.incident_code).group,
                "signs": source.signs,
                "incident_type": "fire",
                "dds": source.ground_truth.dds.value,
                "victims_count": 0,
                "fire": {"object_kind": "мусорный контейнер"},
            },
        },
    )
    assert created.status_code == 201, created.text
    assert fake_db.submission.kio["address"] == "Москва, тестовая улица, дом 3"
    assert created.json()["status"] == "pending"
    assert fake_db.commit_rows[0][0].action == "scenario.submission.create"

    client.post("/api/auth/logout")
    _login(client, "instructor")
    approved = client.post(
        f"/api/scenario-submissions/{created.json()['id']}/review",
        json={"decision": "approve", "comment": "Проверено."},
    )
    assert approved.status_code == 200, approved.text
    assert fake_db.scenario_row.owner_login == "demo-instructor"
    assert fake_db.commit_rows[1][-1].action == "scenario.submission.approve"
    persisted_kio = fake_db.scenario_row.body["student_card"]
    assert persisted_kio["address"] == "Москва, тестовая улица, дом 3"
    assert approved.json()["status"] == "approved"


def test_rejection_requires_comment_and_returns_proposal_to_student(client, monkeypatch):
    audit_rows = []

    async def capture_audit(*args):
        audit_rows.append(args)

    monkeypatch.setattr(scenario_submissions, "audit", capture_audit)
    _login(client, "trainee")
    response = client.post(
        "/api/scenario-submissions",
        json={
            "title": "Обстановка на объекте",
            "level": "L1",
            "kio": _student_kio(
                "В помещении обнаружено повреждение инженерного оборудования."
            ),
        },
    )
    submission_id = response.json()["id"]
    client.post("/api/auth/logout")
    _login(client, "instructor")

    missing_reason = client.post(
        f"/api/scenario-submissions/{submission_id}/review", json={"decision": "reject"}
    )
    assert missing_reason.status_code == 422
    rejected = client.post(
        f"/api/scenario-submissions/{submission_id}/review",
        json={
            "decision": "reject",
            "comment": "Уточните место и наблюдаемые признаки.",
        },
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert audit_rows[-1][-1] == "comment_chars=38"
    assert "Уточните место" not in audit_rows[-1][-1]

    client.post("/api/auth/logout")
    _login(client, "trainee")
    own = client.get("/api/scenario-submissions").json()
    assert own[0]["status"] == "rejected"
    assert own[0]["review_comment"] == "Уточните место и наблюдаемые признаки."
    assert not any("student-created" in item.get("topics", [])
                   for item in client.get("/api/scenarios").json())


def test_submission_validation_rejects_short_description(client):
    _login(client, "trainee")
    response = client.post(
        "/api/scenario-submissions",
        json={
            "title": "Короткая заявка",
            "level": "L1",
            "kio": _student_kio("дым"),
        },
    )
    assert response.status_code == 422


def test_submission_requires_structured_kio_not_legacy_free_text(client):
    _login(client, "trainee")
    response = client.post(
        "/api/scenario-submissions",
        json={
            "title": "Только текст",
            "level": "L1",
            "incident_type": "fire",
            "description": "В мастерской обнаружены дым и повреждение оборудования.",
            "address": "Москва, учебная улица, дом 10",
        },
    )
    assert response.status_code == 422


def test_submission_rejects_whitespace_only_title(client):
    _login(client, "trainee")
    response = client.post(
        "/api/scenario-submissions",
        json={"title": "   ", "level": "L1", "kio": _student_kio()},
    )
    assert response.status_code == 422


def test_submission_uses_street_and_building_when_address_is_blank(client):
    _login(client, "trainee")
    kio = _student_kio()
    kio.update({"address": "   ", "street": "Учебная улица", "building": "12"})
    response = client.post(
        "/api/scenario-submissions",
        json={"title": "Проверка адреса", "level": "L1", "kio": kio},
    )
    assert response.status_code == 201, response.text
    assert response.json()["address"] == "Учебная улица 12"


@pytest.mark.parametrize(
    ("group_id", "group_owner", "detail"),
    [
        (None, None, "trainee_group_required_for_review"),
        (
            UUID("00000000-0000-4000-8000-000000000001"),
            None,
            "instructor_group_required_for_review",
        ),
    ],
)
def test_submission_requires_a_group_with_a_moderating_instructor(
    client,
    monkeypatch,
    group_id,
    group_owner,
    detail,
):
    trainee_id = UUID("00000000-0000-4000-8000-000000000112")
    monkeypatch.setattr(
        scenario_submissions,
        "require",
        lambda *_args, **_kwargs: auth.Principal(
            login="student",
            full_name="Курсант",
            role=auth.Role.TRAINEE,
            trainee_id=trainee_id,
        ),
    )

    class FakeDb:
        async def get(self, model, _key):
            if model.__name__ == "Trainee":
                return SimpleNamespace(group_id=group_id)
            return SimpleNamespace(owner_login=group_owner)

        def add(self, _row):
            raise AssertionError("proposal must not be stored without a moderator")

    async def session_override():
        yield FakeDb()

    app.dependency_overrides[scenario_submissions.submission_session] = session_override
    try:
        response = client.post(
            "/api/scenario-submissions",
            json={
                "title": "Пожар в мастерской",
                "level": "L1",
                "kio": _student_kio(
                    "В мастерской обнаружены дым и повреждение оборудования."
                ),
            },
        )
    finally:
        app.dependency_overrides.pop(scenario_submissions.submission_session, None)

    assert response.status_code == 409
    assert response.json()["detail"] == detail
