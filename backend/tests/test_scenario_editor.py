"""Преподавательский цикл: шаблон → черновик → проверка → утверждение."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.http import scenarios as scenarios_api
from app.config import get_settings
from app.dialog.llm import LlmUnavailable
from app.db.models import AuditLog
from app.main import app
from app.scenarios import generation, store
from app.scenarios.editor import merge_patch, template_copy, validate
from app.scenarios.generation import (
    GenerationError,
    correction_target,
    full_proposal_body,
    parse_full_proposal,
    parse_proposal,
    proposal_body,
    style_fallback,
)
from app.scenarios.loader import ScenarioError, load_file

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


class FakeSession:
    """Минимальная транзакционная замена БД для проверки HTTP без Postgres."""

    def __init__(self):
        self.rows = {}
        self.audit_rows = []
        self.commit_audit_counts = []

    def add(self, row):
        if isinstance(row, AuditLog):
            self.audit_rows.append(row)
        else:
            self.rows[row.id] = row

    async def get(self, model, key):
        return self.rows.get(key)

    async def commit(self):
        self.commit_audit_counts.append(len(self.audit_rows))

    async def scalars(self, query):
        expression = query.column_descriptions[0]["expr"]
        if getattr(expression, "key", None) == "id":
            owner_filter = next(
                clause for clause in query.whereclause.clauses
                if getattr(getattr(clause, "left", None), "key", None) == "owner_login"
                and getattr(getattr(clause, "right", None), "value", None) is not None
            )
            owner_login = owner_filter.right.value
            owner_operator = owner_filter.operator.__name__
            return [
                row.id for row in self.rows.values()
                if row.status == "published"
                and (
                    row.owner_login == owner_login
                    if owner_operator == "eq"
                    else row.owner_login is not None and row.owner_login != owner_login
                )
            ]
        return [row for row in self.rows.values() if row.status == "published"]


@pytest.fixture
def client(monkeypatch):
    db = FakeSession()

    async def no_audit(*args, **kwargs):
        pass

    async def no_restore(*args, **kwargs):
        return 0

    app.dependency_overrides[scenarios_api.scenario_session] = lambda: db
    monkeypatch.setattr(scenarios_api, "audit", no_audit)
    monkeypatch.setattr(store, "restore_published", no_restore)
    with TestClient(app) as test_client:
        test_client.fake_db = db
        yield test_client
    app.dependency_overrides.clear()


def assert_atomic_audit(client, action: str, object_id: str) -> None:
    row = client.fake_db.audit_rows[-1]
    assert row.action == action
    assert row.object_id == object_id
    assert client.fake_db.commit_audit_counts[-1] == len(client.fake_db.audit_rows)


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


def test_editor_can_save_explicit_scenario_decline_with_required_reason():
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    body = template_copy(source, "draft-decline")
    declined = merge_patch(body, {
        "dds_decision": {
            "expected": "decline",
            "reason": "Повторный вызов уже отрабатывается по первой карточке.",
        },
    })
    assert validate(declined).dds_decision.expected == "decline"
    with pytest.raises(ScenarioError, match="reason обязателен"):
        validate(merge_patch(body, {"dds_decision": {"expected": "decline"}}))


def test_ai_proposal_changes_only_story_and_keeps_reference():
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    proposal = parse_proposal('''```json
{"title":"Ночной пожар на балконе","first_line":"Алло, помогите, балкон горит и всё в дыму!",
 "facts":{"f_what_burns":"на балконе открытое пламя","f_smoke":"дым заполнил подъезд"}}
```''', source)
    body = proposal_body(source, "ai-example", proposal)
    generated = validate(body)
    assert generated.facts[0].value == source.facts[0].value
    assert generated.ground_truth.address == source.ground_truth.address
    assert generated.ground_truth.victims == source.ground_truth.victims
    assert generated.ground_truth.incident_code == source.ground_truth.incident_code
    assert generated.facts[1].value == "на балконе открытое пламя"


def test_ai_proposal_rejects_address_change_and_premature_number():
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    with pytest.raises(GenerationError, match="защищённые"):
        parse_proposal('{"title":"Другой пожар","first_line":"Алло, помогите, у нас горит балкон!",'
                       '"facts":{"f_address":"другой адрес"}}', source)
    with pytest.raises(GenerationError, match="числовые"):
        parse_proposal('{"title":"Другой пожар","first_line":"Горит дом 99, срочно приезжайте!",'
                       '"facts":{}}', source)
    with pytest.raises(GenerationError, match="числовые"):
        parse_proposal('{"title":"Другой пожар","first_line":"Один человек пострадал, помогите!",'
                       '"facts":{}}', source)
    with pytest.raises(GenerationError, match="вопрос вместо ответа"):
        parse_proposal('{"title":"Другой пожар","first_line":"Помогите, на балконе пожар!",'
                       '"facts":{"f_smoke":"Куда идёт дым?"}}', source)
    with pytest.raises(GenerationError, match="плейсхолдер"):
        parse_proposal('{"title":"Другой пожар","first_line":"Помогите, на балконе пожар!",'
                       '"facts":{"f_smoke":"{f_smoke}"}}', source)
    with pytest.raises(GenerationError, match="новых обстоятельств"):
        parse_proposal('{"title":"Другой пожар","first_line":"Помогите, на балконе пожар!",'
                       '"facts":{}}', source)
    revised = parse_proposal(
        '{"title":"Пожар — короткая реплика","first_line":"Пожар! Помогите!","facts":{}}',
        source, require_fact_change=False, instruction="Сделай реплику короче",
        restrict_first_line_facts=True,
    )
    assert revised["first_line"] == "Пожар! Помогите!"
    with pytest.raises(GenerationError, match="не запрошенные"):
        parse_proposal(
            '{"title":"Пожар — короткая реплика",'
            '"first_line":"Горим! Не успел перекрыть газ!","facts":{}}',
            source, require_fact_change=False, instruction="Сделай реплику короче",
            restrict_first_line_facts=True,
        )


def test_revision_target_is_selected_only_when_comment_names_a_fact():
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    assert correction_target(source, "Сделай дым чёрным и плотным") == "f_smoke"
    assert correction_target(source, "Сделай первую реплику короче и тревожнее") is None
    shortened = style_fallback(source, "Сделай первую реплику короче")
    assert shortened["first_line"] == "Помогите! Горим!"
    assert style_fallback(source, "Сделай тон спокойнее") is None


def test_full_ai_proposal_replaces_all_facts_and_derives_soft_truth():
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    raw = '''{
      "title":"Пожар на балконе жилого дома",
      "first_line":"Помогите, с балкона валит чёрный дым!",
      "facts":{
        "f_address":"улица Новая, 8, корпус 2, первый этаж",
        "f_what_burns":"на балконе жилого дома видно открытое пламя",
        "f_people":"все вышли; Пострадавших: 0",
        "f_smoke":"густой чёрный дым идёт в лестничный холл",
        "f_gas":"газового оборудования в квартире нет"
      },
      "ground_truth":{"address":"улица Новая, 8","victims":0}
    }'''
    proposal = parse_full_proposal(raw, source)
    body = full_proposal_body(source, "ai-full-example", proposal)
    generated = validate(body)
    assert generated.ground_truth.address == "улица Новая, 8"
    assert generated.ground_truth.victims == 0
    assert generated.ground_truth.incident_code == source.ground_truth.incident_code
    assert generated.facts[0].value.startswith("улица Новая")
    assert all(fact.refined is None and fact.refine_on is None for fact in generated.facts)
    assert parse_full_proposal(raw.replace("Пострадавших: 0", "пострадавших нет"), source)

    inconsistent = raw.replace("Пожар на балконе жилого дома", "Пожар в мастерской") \
        .replace("с балкона", "из мастерской") \
        .replace("на балконе жилого дома видно открытое пламя", "горит оборудование мастерской")
    with pytest.raises(ScenarioError, match="сюжет не подтверждает путь ЕКП"):
        full_proposal_body(source, "ai-full-inconsistent", parse_full_proposal(inconsistent, source))

    with pytest.raises(GenerationError, match="числа пострадавших"):
        parse_full_proposal(raw.replace("Пострадавших: 0", "Пострадавших не видно"), source)
    with pytest.raises(GenerationError, match="адрес не совпадает"):
        parse_full_proposal(raw.replace('"address":"улица Новая, 8"',
                                        '"address":"улица Другая, 9"'), source)


@pytest.mark.asyncio
async def test_ai_generation_retries_copied_facts_with_strict_schema(monkeypatch):
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    requests = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.answers = iter([
                ('{"title":"Ночной пожар","first_line":"Алло, на балконе горит!",'
                 '"facts":{"f_smoke":"дым идёт в подъезд, на площадке ничего не видно"}}'),
                ('{"title":"Ночной пожар","first_line":"Алло, на балконе горит!",'
                 '"facts":{"f_smoke":"лестница уже заполнена густым дымом"}}'),
            ])

        async def complete(self, request, **kwargs):
            requests.append(request)
            return next(self.answers)

        async def aclose(self):
            pass

    monkeypatch.setattr(generation, "LlmClient", FakeClient)
    proposal = await generation.generate(
        source, "Ночной пожар на балконе, густой дым в подъезде, заявитель паникует"
    )
    assert proposal["facts"]["f_smoke"] == "лестница уже заполнена густым дымом"
    assert len(requests) == 2
    assert requests[0].response_format["type"] == "json_object"
    assert set(requests[0].response_format["schema"]["properties"]["facts"]["properties"]) == {"f_smoke"}
    assert "f_smoke" in requests[1].messages[-1]["content"]


@pytest.mark.asyncio
async def test_ai_generation_retries_one_qwen_control_token_failure(monkeypatch):
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)

    class FakeClient:
        calls = 0

        def __init__(self, **kwargs):
            pass

        async def complete(self, request, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise LlmUnavailable("ответ содержит служебные токены модели")
            return ('{"title":"Ночной дым","first_line":"Алло, всё в дыму, помогите!",'
                    '"facts":{"f_smoke":"лестница заполнена густым дымом"}}')

        async def aclose(self):
            pass

    monkeypatch.setattr(generation, "LlmClient", FakeClient)
    proposal = await generation.generate(source, "Сделай дым густым и чёрным")
    assert proposal["facts"]["f_smoke"].startswith("лестница")


@pytest.mark.asyncio
async def test_description_generation_asks_small_model_one_fact_at_a_time(monkeypatch):
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    answers = iter([
        '{"title":"Пожар на балконе жилого дома","first_line":"С балкона идёт дым"}',
        '{"value":"ул. Маршака, д. 12, этаж 1","address":"ул. Маршака, д. 12"}',
        '{"value":"на балконе жилого дома видно открытое пламя"}',
        '{"value":"все люди успели выйти","victims":0}',
        '{"value":"густой дым вышел в подъезд"}',
        '{"value":"газового оборудования нет, но нужно всё проверить"}',
    ])
    requests = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def complete(self, request, **kwargs):
            requests.append(request)
            return next(answers)

        async def aclose(self):
            pass

    monkeypatch.setattr(generation, "LlmClient", FakeClient)
    proposal = await generation.generate_from_description(
        source, "Пожар в мастерской, все вышли, густой дым в подъезде"
    )
    assert len(requests) == 6
    assert requests[0].messages[-1]["content"].find("classifier_signs") >= 0
    assert all(
        "classifier_signs" in request.messages[1]["content"]
        for request in requests[1:]
    )
    assert proposal["first_line"].endswith("!")
    assert proposal["ground_truth"] == {
        "address": "ул. Маршака, д. 12, этаж 1", "victims": 0,
    }
    assert proposal["facts"]["f_people"].endswith("Пострадавших: 0")
    assert proposal["facts"]["f_gas"] == "газового оборудования нет"


@pytest.mark.asyncio
async def test_description_generation_retries_fact_that_is_a_question(monkeypatch):
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    answers = iter([
        '{"title":"Пожар на балконе жилого дома","first_line":"С балкона идёт дым"}',
        '{"value":"ул. Маршака, д. 12, этаж 1","address":"ул. Маршака, д. 12"}',
        '{"value":"на балконе жилого дома видно открытое пламя"}',
        '{"value":"Есть ли пострадавшие?","victims":1}',
        '{"value":"один человек получил ожог; Пострадавших: 1","victims":1}',
        '{"value":"густой дым вышел в подъезд"}',
        '{"value":"газового оборудования нет"}',
    ])
    requests = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def complete(self, request, **kwargs):
            requests.append(request)
            return next(answers)

        async def aclose(self):
            pass

    monkeypatch.setattr(generation, "LlmClient", FakeClient)
    proposal = await generation.generate_from_description(
        source, "Пожар в мастерской, один человек получил ожог, густой дым в подъезде"
    )
    assert len(requests) == 7
    assert "Ответ отклонён" in requests[4].messages[-1]["content"]
    assert proposal["ground_truth"]["victims"] == 1
    assert proposal["facts"]["f_people"].endswith("Пострадавших: 1")


def test_draft_is_hidden_until_approval_and_then_available_to_lesson(client, monkeypatch):
    from app.scoring.grammar import GrammarAssessment

    async def fake_assess(_text):
        return GrammarAssessment(True, (), "rules")

    monkeypatch.setattr(scenarios_api, "assess", fake_assess)
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
    assert_atomic_audit(client, "scenario.draft.create", draft_id)
    assert store.get(draft_id) is None
    assert client.get(f"/api/scenarios/{draft_id}").status_code == 404

    changed = client.patch(
        f"/api/scenarios/drafts/{draft_id}",
        json={"first_line": "Соседи! В доме дым!"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["body"]["first_line"] == "Соседи! В доме дым!"
    assert_atomic_audit(client, "scenario.draft.update", draft_id)
    check = client.post(f"/api/scenarios/drafts/{draft_id}/validate")
    assert check.status_code == 200 and check.json()["valid"]
    grammar = client.post(f"/api/scenarios/drafts/{draft_id}/grammar-check")
    assert grammar.status_code == 200 and grammar.json()["valid"]
    approved = client.post(f"/api/scenarios/drafts/{draft_id}/approve")
    assert approved.status_code == 200, approved.text
    assert_atomic_audit(client, "scenario.approve", draft_id)
    assert approved.json()["status"] == "published"
    assert store.get(draft_id).first_line == "Соседи! В доме дым!"
    assert client.get(f"/api/scenarios/{draft_id}").status_code == 200
    assert client.patch(f"/api/scenarios/drafts/{draft_id}", json={"title": "нет"}).status_code == 404


def test_ai_draft_requires_instructor_review_before_publication(client, monkeypatch):
    async def fake_generate(source, instruction, **kwargs):
        assert source.id == "fire-apartment-l2"
        assert "ночью" in instruction
        return {"title": "Ночной пожар на балконе",
                "first_line": "Помогите, у нас горит балкон!",
                "facts": {"f_smoke": "густой дым уже в подъезде"}}

    monkeypatch.setattr(scenarios_api, "generate", fake_generate)
    client.post("/api/auth/dev-token")
    response = client.post("/api/scenarios/drafts/generate", json={
        "source_id": "fire-apartment-l2", "instruction": "Пожар начался ночью",
    })
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["generation"] == "ai_variant"
    assert draft["id"].startswith("ai-")
    assert_atomic_audit(client, "scenario.draft.ai_generate", draft["id"])
    assert draft["body"]["first_line"] == "Помогите, у нас горит балкон!"
    assert store.get(draft["id"]) is None
    assert client.get(f"/api/scenarios/{draft['id']}").status_code == 404
    assert client.post(f"/api/scenarios/drafts/{draft['id']}/validate").json()["valid"]
    assert client.post(f"/api/scenarios/drafts/{draft['id']}/approve").status_code == 200
    assert store.get(draft["id"]).first_line == "Помогите, у нас горит балкон!"


def test_description_generates_new_story_and_reference_answers(client, monkeypatch):
    async def fake_generate(source, description):
        assert source.id == "fire-apartment-l2"
        assert "мастерской" in description
        return {
            "title": "Пожар на балконе жилого дома",
            "first_line": "Помогите, с балкона валит чёрный дым!",
            "facts": {
                "f_address": "улица Новая, 8, корпус 2, первый этаж",
                "f_what_burns": "на балконе жилого дома видно открытое пламя",
                "f_people": "все вышли; Пострадавших: 0",
                "f_smoke": "густой чёрный дым идёт в лестничный холл",
                "f_gas": "газового оборудования в квартире нет",
            },
            "ground_truth": {"address": "улица Новая, 8", "victims": 0},
        }

    monkeypatch.setattr(scenarios_api, "generate_from_description", fake_generate)
    client.post("/api/auth/dev-token")
    response = client.post("/api/scenarios/drafts/generate-from-description", json={
        "source_id": "fire-apartment-l2",
        "description": "Пожар в мастерской жилого дома, все успели выйти",
    })
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["generation"] == "ai_full"
    assert draft["id"].startswith("ai-full-")
    assert draft["body"]["ground_truth"] == {"address": "улица Новая, 8", "victims": 0}
    assert client.post(f"/api/scenarios/drafts/{draft['id']}/validate").json()["valid"]
    assert store.get(draft["id"]) is None


def test_instructor_revises_same_ai_draft_by_comment(client, monkeypatch):
    answers = iter([
        {"title": "Ночной пожар на балконе",
         "first_line": "Помогите, на балконе горит!",
         "facts": {"f_smoke": "густой дым уже в подъезде"}},
        {"title": "Ночной пожар с сильным дымом",
         "first_line": "Алло, лестница вся в дыму, помогите!",
         "facts": {"f_smoke": "чёрный дым полностью закрыл лестничную площадку"}},
    ])
    comments = []

    async def fake_generate(source, instruction, **kwargs):
        comments.append(instruction)
        if len(comments) > 1:
            assert kwargs["require_fact_change"] is False
        return next(answers)

    monkeypatch.setattr(scenarios_api, "generate", fake_generate)
    client.post("/api/auth/dev-token")
    created = client.post("/api/scenarios/drafts/generate", json={
        "source_id": "fire-apartment-l2", "instruction": "Добавить густой ночной дым",
    }).json()
    draft_id = created["id"]
    revised = client.post(f"/api/scenarios/drafts/{draft_id}/revise", json={
        "comment": "Сделай дым чёрным и закрой им площадку",
    })
    assert revised.status_code == 200, revised.text
    body = revised.json()["body"]
    assert revised.json()["id"] == draft_id
    assert body["title"] == "Ночной пожар с сильным дымом"
    assert body["facts"][0]["value"] == "улица Ленина, 14, квартира 47, 5-й этаж"
    assert next(item["value"] for item in body["facts"] if item["id"] == "f_smoke").startswith("чёрный")
    assert comments[-1] == "Сделай дым чёрным и закрой им площадку"
    assert_atomic_audit(client, "scenario.draft.ai_revise", draft_id)
    audit_row = client.fake_db.audit_rows[-1]
    assert audit_row.detail == "instruction_chars=38"
    assert "чёрным" not in audit_row.detail
    assert client.post(f"/api/scenarios/drafts/{draft_id}/validate").json()["valid"]


def test_manual_grammar_check_covers_caller_line_and_fact_values(client, monkeypatch):
    from app.scoring.grammar import GrammarAssessment

    checked = []

    async def fake_assess(text):
        checked.append(text)
        return GrammarAssessment(True, (), "rules")

    monkeypatch.setattr(scenarios_api, "assess", fake_assess)
    client.post("/api/auth/dev-token")
    created = client.post("/api/scenarios/drafts/from-template", json={
        "source_id": "fire-apartment-l2",
    })
    assert created.status_code == 201, created.text
    draft_id = created.json()["id"]

    changed = client.patch(
        f"/api/scenarios/drafts/{draft_id}",
        json={"first_line": "Помогите! Горит балкон."},
    )
    assert changed.status_code == 200, changed.text
    assert client.post(f"/api/scenarios/drafts/{draft_id}/approve").status_code == 409

    response = client.post(f"/api/scenarios/drafts/{draft_id}/grammar-check")

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["valid"] is True
    assert result["checks"][0]["field"] == "first_line"
    assert len(result["checks"]) == 1 + len(changed.json()["body"]["facts"])
    assert checked == [changed.json()["body"]["first_line"], *[
        fact["value"] for fact in changed.json()["body"]["facts"]
    ]]
    assert client.post(f"/api/scenarios/drafts/{draft_id}/approve").status_code == 200


def test_failed_grammar_check_does_not_authorize_manual_draft(client, monkeypatch):
    from app.scoring.grammar import GrammarAssessment

    async def fake_assess(_text):
        return GrammarAssessment(False, ("тестовая языковая ошибка",), "rules")

    monkeypatch.setattr(scenarios_api, "assess", fake_assess)
    client.post("/api/auth/dev-token")
    created = client.post("/api/scenarios/drafts/from-template", json={
        "source_id": "fire-apartment-l2",
    })
    draft_id = created.json()["id"]
    assert client.patch(f"/api/scenarios/drafts/{draft_id}",
                        json={"first_line": "пожар"}).status_code == 200

    result = client.post(f"/api/scenarios/drafts/{draft_id}/grammar-check")

    assert result.status_code == 200
    assert result.json()["valid"] is False
    assert result.json()["checks"][0]["errors"] == ["тестовая языковая ошибка"]
    assert client.post(f"/api/scenarios/drafts/{draft_id}/approve").status_code == 409


def test_ai_editor_works_in_demo_lite_without_database(monkeypatch):
    monkeypatch.setenv("DEMO_NO_DB", "true")
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    get_settings.cache_clear()

    async def fake_generate(source, instruction, **kwargs):
        return {"title": "Ночной дым на балконе",
                "first_line": "Алло, помогите, на балконе горит!",
                "facts": {"f_smoke": "лестница уже заполнена густым дымом"}}

    monkeypatch.setattr(scenarios_api, "generate", fake_generate)
    try:
        with TestClient(app) as demo:
            assert demo.post("/api/auth/dev-token").status_code == 200
            created = demo.post("/api/scenarios/drafts/generate", json={
                "source_id": "fire-apartment-l2", "instruction": "Добавить густой ночной дым",
            })
            assert created.status_code == 201, created.text
            draft_id = created.json()["id"]
            assert demo.get(f"/api/scenarios/drafts/{draft_id}").status_code == 200
            assert demo.post(f"/api/scenarios/drafts/{draft_id}/validate").json()["valid"]
            assert demo.post(f"/api/scenarios/drafts/{draft_id}/approve").status_code == 200
            assert demo.get(f"/api/scenarios/{draft_id}").status_code == 200
    finally:
        get_settings.cache_clear()


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


def test_unresolved_fact_placeholder_cannot_be_validated_or_approved(client):
    client.post("/api/auth/dev-token")
    created = client.post(
        "/api/scenarios/drafts/from-template",
        json={"source_id": "fire-apartment-l2"},
    ).json()
    draft_id = created["id"]
    facts = created["body"]["facts"]
    facts[1]["value"] = "{f_what_burns}"
    assert client.patch(
        f"/api/scenarios/drafts/{draft_id}", json={"facts": facts}
    ).status_code == 200
    check = client.post(f"/api/scenarios/drafts/{draft_id}/validate")
    assert check.json()["valid"] is False
    assert "плейсхолдер" in check.json()["errors"][0]
    assert client.post(f"/api/scenarios/drafts/{draft_id}/approve").status_code == 422


def test_unauthenticated_user_cannot_manage_drafts(client):
    response = client.post(
        "/api/scenarios/drafts/from-template", json={"source_id": "fire-apartment-l2"}
    )
    assert response.status_code == 401


def test_instructor_archives_and_restores_scenario_without_deleting_history(client):
    client.post("/api/auth/dev-token")
    created = client.post(
        "/api/scenarios/drafts/from-template",
        json={"source_id": "fire-apartment-l2", "title": "Сценарий для проверки архива"},
    )
    assert created.status_code == 201, created.text
    scenario_id = created.json()["id"]
    approved = client.post(f"/api/scenarios/drafts/{scenario_id}/approve")
    assert approved.status_code == 200, approved.text
    assert_atomic_audit(client, "scenario.approve", scenario_id)
    original = store.get(scenario_id)
    assert original is not None

    archived = client.delete(f"/api/scenarios/{scenario_id}")
    assert archived.status_code == 200, archived.text
    assert_atomic_audit(client, "scenario.archive", scenario_id)
    assert archived.json()["status"] == "archived"
    assert store.get(scenario_id) is None
    assert scenario_id not in {item["id"] for item in client.get("/api/scenarios").json()}
    assert client.get(f"/api/scenarios/{scenario_id}").status_code == 404

    restored = client.post(f"/api/scenarios/{scenario_id}/restore")
    assert restored.status_code == 200, restored.text
    assert_atomic_audit(client, "scenario.restore", scenario_id)
    assert restored.json()["status"] == "published"
    assert store.get(scenario_id).title == original.title
    assert scenario_id in {item["id"] for item in client.get("/api/scenarios").json()}


def test_instructor_cannot_read_or_edit_another_instructors_draft(client, monkeypatch):
    from app.api.auth import Principal
    from app.domain.roles import Role

    identity = {"login": "teacher-one"}

    def instructor(_request, *_roles):
        return Principal(login=identity["login"], full_name="Преподаватель", role=Role.INSTRUCTOR)

    monkeypatch.setattr(scenarios_api, "require", instructor)
    created = client.post(
        "/api/scenarios/drafts/from-template",
        json={"source_id": "fire-apartment-l2", "title": "Личный черновик"},
    )
    assert created.status_code == 201, created.text
    scenario_id = created.json()["id"]

    identity["login"] = "teacher-two"
    assert client.get(f"/api/scenarios/drafts/{scenario_id}").status_code == 404
    assert client.patch(
        f"/api/scenarios/drafts/{scenario_id}", json={"title": "Чужая правка"}
    ).status_code == 404
    assert client.post(f"/api/scenarios/drafts/{scenario_id}/approve").status_code == 404


def test_instructor_cannot_archive_another_instructors_published_scenario(client, monkeypatch):
    from app.api.auth import Principal
    from app.domain.roles import Role

    identity = {"login": "teacher-one"}

    def instructor(_request, *_roles):
        return Principal(login=identity["login"], full_name="Преподаватель", role=Role.INSTRUCTOR)

    monkeypatch.setattr(scenarios_api, "require", instructor)
    created = client.post(
        "/api/scenarios/drafts/from-template",
        json={"source_id": "fire-apartment-l2", "title": "Публикация автора"},
    )
    assert created.status_code == 201, created.text
    scenario_id = created.json()["id"]
    assert client.post(f"/api/scenarios/drafts/{scenario_id}/approve").status_code == 200
    scenario = next(item for item in client.get("/api/scenarios").json() if item["id"] == scenario_id)
    assert scenario["can_manage"] is True

    identity["login"] = "teacher-two"
    assert scenario_id not in {item["id"] for item in client.get("/api/scenarios").json()}
    assert client.get(f"/api/scenarios/{scenario_id}").status_code == 404
    assert client.delete(f"/api/scenarios/{scenario_id}").status_code == 404


def test_scenario_in_active_lesson_cannot_be_archived(client, monkeypatch):
    client.post("/api/auth/dev-token")
    monkeypatch.setattr(scenarios_api.hub, "has_active_scenario", lambda _scenario_id: True)
    response = client.delete("/api/scenarios/fire-apartment-l2")
    assert response.status_code == 409
    assert response.json()["detail"] == "scenario_is_used_by_active_session"


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
