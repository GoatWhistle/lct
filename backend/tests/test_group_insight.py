"""ИИ-инсайт получает только агрегаты и обязан вернуть строгий JSON."""

import json

import pytest

from app.config import get_settings
from app.scoring import group_insight


@pytest.fixture(autouse=True)
def local_settings(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "local")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:18080/v1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_insight_uses_only_aggregates_and_strict_schema(monkeypatch):
    seen = []

    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def complete(self, request, **_kwargs):
            seen.append(request)
            return json.dumps({
                "summary": "Группа чаще всего теряет обязательные сведения при опросе.",
                "priorities": ["Отработать уточнение адреса и этажа."],
            }, ensure_ascii=False)
        async def aclose(self): pass

    monkeypatch.setattr(group_insight, "LlmClient", FakeClient)
    result = await group_insight.generate_group_insight({
        "active_trainees": 4,
        "scored_attempts": 7,
        "average_score": 76.2,
        "errors": [{
            "code": "E2", "title": "Пропущен обязательный факт",
            "rate_percent": 50.0, "recommendation": "Повторить алгоритм опроса",
        }],
    })
    assert result["priorities"] == ["Отработать уточнение адреса и этажа."]
    request = seen[0]
    assert request.temperature == 0
    assert request.response_format["schema"]["additionalProperties"] is False
    payload = json.loads(request.messages[1]["content"])
    assert set(payload) == {"active_trainees", "scored_attempts", "average_score", "typical_errors"}
    assert "trainee_id" not in json.dumps(payload)
    assert "group" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_insight_rejects_wrong_shape(monkeypatch):
    class FakeClient:
        def __init__(self, **_kwargs): pass
        async def complete(self, *_args, **_kwargs):
            return '{"summary":"слишком кратко","priorities":[],"secret":"x"}'
        async def aclose(self): pass

    monkeypatch.setattr(group_insight, "LlmClient", FakeClient)
    with pytest.raises(group_insight.InsightInvalid):
        await group_insight.generate_group_insight({
            "active_trainees": 1, "scored_attempts": 1, "average_score": 50,
            "errors": [],
        })
