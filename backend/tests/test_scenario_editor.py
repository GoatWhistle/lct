"""Преподавательский цикл: шаблон → черновик → проверка → утверждение."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.http import scenarios as scenarios_api
from app.db.base import get_session
from app.main import app
from app.scenarios import store
from app.scenarios.editor import merge_patch, template_copy, validate
from app.scenarios.loader import ScenarioError, load_file

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


class FakeSession:
    """Минимальная транзакционная замена БД для проверки HTTP без Postgres."""

    def __init__(self):
        self.rows = {}

    def add(self, row):
        self.rows[row.id] = row

    async def get(self, model, key):
        return self.rows.get(key)

    async def commit(self):
        pass

    async def scalars(self, query):
        return [row for row in self.rows.values() if row.status == "published"]


@pytest.fixture
def client(monkeypatch):
    db = FakeSession()

    async def no_audit(*args, **kwargs):
        pass

    async def no_restore(*args, **kwargs):
        return 0

    app.dependency_overrides[get_session] = lambda: db
    monkeypatch.setattr(scenarios_api, "audit", no_audit)
    monkeypatch.setattr(store, "restore_published", no_restore)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_template_copy_is_local_independent_and_valid():
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    body = template_copy(source, "draft-example")
    assert body["id"] == "draft-example"
    assert body["extends"] is None
    assert body["ticket"] is None
    assert body["ground_truth"].keys() == {"address", "victims"}
    assert validate(body).ground_truth.dds == source.ground_truth.dds
    assert source.id != body["id"]  # оригинал не меняется


def test_editor_rejects_derived_truth_and_missing_fact():
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    body = template_copy(source, "draft-example")
    with pytest.raises(ScenarioError, match="ground_truth"):
        merge_patch(body, {"ground_truth": {"dds": "02"}})
    with pytest.raises(ScenarioError, match="id сценария"):
        merge_patch(body, {"id": "other"})
    broken = merge_patch(body, {"facts": []})
    with pytest.raises(ScenarioError, match="нужны факты"):
        validate(broken)


def test_draft_is_hidden_until_approval_and_then_available_to_lesson(client):
    source = next(s for s in store.all_scenarios() if s.id == "fire-apartment-l2")
    assert client.post("/api/auth/dev-token").status_code == 200
    response = client.post(
        "/api/scenarios/drafts/from-template",
        json={"source_id": source.id, "title": "Учебная копия"},
    )
    assert response.status_code == 201, response.text
    draft = response.json()
    draft_id = draft["id"]
    assert draft["generation"] == "template_copy"
    assert draft["status"] == "draft"
    assert store.get(draft_id) is None
    assert client.get(f"/api/scenarios/{draft_id}").status_code == 404

    changed = client.patch(
        f"/api/scenarios/drafts/{draft_id}",
        json={"first_line": "Соседи! В доме дым!"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["body"]["first_line"] == "Соседи! В доме дым!"
    check = client.post(f"/api/scenarios/drafts/{draft_id}/validate")
    assert check.status_code == 200 and check.json()["valid"]
    approved = client.post(f"/api/scenarios/drafts/{draft_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "published"
    assert store.get(draft_id).first_line == "Соседи! В доме дым!"
    assert client.get(f"/api/scenarios/{draft_id}").status_code == 200
    assert client.patch(f"/api/scenarios/drafts/{draft_id}", json={"title": "нет"}).status_code == 404


def test_invalid_draft_cannot_be_approved(client):
    client.post("/api/auth/dev-token")
    response = client.post(
        "/api/scenarios/drafts/from-template",
        json={"source_id": "fire-apartment-l2"},
    )
    draft_id = response.json()["id"]
    assert client.patch(f"/api/scenarios/drafts/{draft_id}", json={"facts": []}).status_code == 200
    check = client.post(f"/api/scenarios/drafts/{draft_id}/validate")
    assert check.json()["valid"] is False
    assert client.post(f"/api/scenarios/drafts/{draft_id}/approve").status_code == 422
    assert store.get(draft_id) is None


def test_unauthenticated_user_cannot_manage_drafts(client):
    response = client.post(
        "/api/scenarios/drafts/from-template", json={"source_id": "fire-apartment-l2"}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_approved_scenario_restores_from_local_database():
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    db = FakeSession()
    row = await store.create_draft(db, source=source)
    approved = await store.approve_draft(db, row)
    store.set_library([source])  # имитация нового процесса после чтения YAML
    assert store.get(approved.id) is None
    assert await store.restore_published(db) == 1
    assert store.get(approved.id).title == approved.title
    assert await store.restore_published(db) == 0
