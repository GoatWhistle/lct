"""Локальный Qwen-инсайт по обезличенной агрегированной аналитике группы."""

import json

from app.config import get_settings
from app.dialog.llm import LlmClient, LlmRequest, LlmUnavailable, is_loopback_url


class InsightInvalid(ValueError):
    pass


async def generate_group_insight(data: dict) -> dict:
    settings = get_settings()
    if (not settings.llm_model_caller or not is_loopback_url(
        settings.llm_base_url, allow_docker_host=settings.allow_docker_host_models
    )):
        raise LlmUnavailable("локальная модель аналитики не настроена")
    schema = {"type": "json_object", "schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "minLength": 20, "maxLength": 700},
            "priorities": {
                "type": "array", "minItems": 1, "maxItems": 3,
                "items": {"type": "string", "minLength": 8, "maxLength": 240},
            },
        },
        "required": ["summary", "priorities"],
        "additionalProperties": False,
    }}
    prompt = {
        "active_trainees": data["active_trainees"],
        "scored_attempts": data["scored_attempts"],
        "average_score": data["average_score"],
        "typical_errors": [{
            "code": item["code"],
            "title": item["title"],
            "rate_percent": item["rate_percent"],
            "rule_recommendation": item["recommendation"],
        } for item in data["errors"][:8]],
    }
    request = LlmRequest(
        model=settings.llm_model_caller,
        messages=[{
            "role": "system",
            "content": (
                "Ты методист учебного центра системы-112. По обезличенной агрегированной "
                "статистике сформулируй краткий русский вывод и от одной до трёх конкретных "
                "тем следующего занятия. Не придумывай персональные данные, новые числа, "
                "диагнозы или нормативы. Верни только JSON по заданной схеме. /no_think"
            ),
        }, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
        temperature=0.0,
        max_tokens=420,
        response_format=schema,
        strip_reasoning=True,
    )
    client = LlmClient(base_url=settings.llm_base_url, timeout=25)
    try:
        raw = await client.complete(request, use_cache=True)
    finally:
        await client.aclose()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InsightInvalid("модель вернула не JSON") from exc
    if set(payload) != {"summary", "priorities"}:
        raise InsightInvalid("неверный набор полей инсайта")
    summary = payload["summary"]
    priorities = payload["priorities"]
    if not isinstance(summary, str) or not 20 <= len(summary.strip()) <= 700:
        raise InsightInvalid("неверная длина вывода")
    if (not isinstance(priorities, list) or not 1 <= len(priorities) <= 3
            or any(not isinstance(item, str) or not 8 <= len(item.strip()) <= 240
                   for item in priorities)):
        raise InsightInvalid("неверные приоритеты")
    return {"summary": summary.strip(), "priorities": [item.strip() for item in priorities]}
