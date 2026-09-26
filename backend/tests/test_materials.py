"""Учебные материалы: создание, назначение, изучение и безопасная загрузка."""

import base64
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.session.hub import hub


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DEMO_NO_DB", "true")
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    get_settings.cache_clear()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        get_settings.cache_clear()


def _instructor(client: TestClient) -> None:
    response = client.post("/api/auth/dev-token")
    assert response.status_code == 200


def _trainee(client: TestClient) -> None:
    client.post("/api/auth/logout")
    response = client.post(
        "/api/auth/login", json={"login": "demo-trainee", "password": "demo"}
    )
    assert response.status_code == 200


def test_teacher_creates_assigns_and_trainee_completes_text_material(client):
    _instructor(client)
    created = client.post("/api/materials", json={
        "title": "Порядок доклада старшему",
        "description": "Перед практическим занятием",
        "level": "L2",
        "kind": "text",
        "body": "Передайте адрес, тип события, задачу и подтвердите выезд.",
        "scenario_id": "fire-apartment-l2",
    })
    assert created.status_code == 201, created.text
    material_id = created.json()["id"]
    assert created.json()["assignment_count"] == 0

    assigned = client.put(
        f"/api/materials/{material_id}/assign/00000000-0000-4000-8000-000000000112"
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["assigned_at"]

    _trainee(client)
    listing = client.get("/api/materials")
    assert listing.status_code == 200
    item = next(item for item in listing.json() if item["id"] == material_id)
    assert item["body"].startswith("Передайте адрес")
    assert item["completed_at"] is None

    completed = client.post(f"/api/materials/{material_id}/complete")
    assert completed.status_code == 200
    assert completed.json()["completed_at"]
    assert client.post("/api/materials", json={
        "title": "Нельзя создать", "kind": "text", "body": "запрещено",
    }).status_code == 403


def test_teacher_cannot_edit_archive_or_assign_another_teachers_material(client, monkeypatch):
    from app.api.auth import Principal
    from app.api.http import materials as materials_api
    from app.domain.roles import Role

    identity = {"login": "teacher-one"}

    def instructor(_request, *_roles):
        return Principal(login=identity["login"], full_name="Преподаватель", role=Role.INSTRUCTOR)

    monkeypatch.setattr(materials_api, "require", instructor)
    created = client.post("/api/materials", json={
        "title": "Личный материал",
        "level": "L1",
        "kind": "text",
        "body": "Учебный текст.",
    })
    assert created.status_code == 201, created.text
    material_id = created.json()["id"]

    identity["login"] = "teacher-two"
    assert client.patch(f"/api/materials/{material_id}", json={"title": "Подмена"}).status_code == 404
    assert client.delete(f"/api/materials/{material_id}").status_code == 404
    assert client.put(
        f"/api/materials/{material_id}/assign/00000000-0000-4000-8000-000000000112"
    ).status_code == 404
    assert client.delete(
        f"/api/materials/{material_id}/assign/00000000-0000-4000-8000-000000000112"
    ).status_code == 404


def test_uploaded_file_is_limited_sanitized_and_downloaded_as_attachment(client):
    _instructor(client)
    content = b"local training resource\n"
    created = client.post("/api/materials", json={
        "title": "Локальная памятка PDF",
        "kind": "file",
        "file_name": "C:\\Users\\teacher\\guide.txt",
        "media_type": "text/plain",
        "content_base64": base64.b64encode(content).decode(),
    })
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["file_name"] == "guide.txt"
    assert data["file_size"] == len(content)
    assert len(data["file_sha256"]) == 64

    downloaded = client.get(f"/api/materials/{data['id']}/download")
    assert downloaded.status_code == 200
    assert downloaded.content == content
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert downloaded.headers["content-disposition"].startswith("attachment")


def test_unassigned_trainee_cannot_download_resource(client):
    _instructor(client)
    created = client.post("/api/materials", json={
        "title": "Закрытый ресурс",
        "kind": "file",
        "file_name": "private.pdf",
        "media_type": "application/pdf",
        "content_base64": base64.b64encode(b"%PDF-demo").decode(),
    })
    material_id = created.json()["id"]
    _trainee(client)
    assert client.get(f"/api/materials/{material_id}/download").status_code == 403


def test_archive_hides_material_from_trainee_but_keeps_record(client):
    _instructor(client)
    created = client.post("/api/materials", json={
        "title": "Архивируемая памятка",
        "kind": "text",
        "body": "Уникальный тестовый материал для проверки архивации.",
    })
    assert created.status_code == 201, created.text
    material_id = created.json()["id"]

    archived = client.delete(f"/api/materials/{material_id}")
    assert archived.status_code == 200
    assert archived.json()["active"] is False
    assert all(item["id"] != material_id for item in client.get("/api/materials").json())
    archived_list = client.get("/api/materials?include_archived=true").json()
    archived_item = next(item for item in archived_list if item["id"] == material_id)
    assert archived_item["active"] is False

    _trainee(client)
    assert all(item["id"] != material_id for item in client.get("/api/materials").json())


def test_trainee_starts_assigned_practice_in_self_mode(client):
    _trainee(client)
    seeded = client.get("/api/materials").json()[0]
    started = client.post(f"/api/materials/{seeded['id']}/start")
    assert started.status_code == 200, started.text
    payload = started.json()
    assert payload["mode"] == "self"
    assert payload["exercise"] == "card"
    assert payload["path"].startswith("/trainee?session=")
    state = hub.get(UUID(payload["session_id"]))
    assert state is not None
    assert state.trainee_id.hex == "00000000000040008000000000000112"
    hub.drop(state.session_id)
