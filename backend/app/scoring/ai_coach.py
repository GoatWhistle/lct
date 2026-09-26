"""Optional local-model coaching based only on deterministic score findings.

The model may explain how to improve, but never changes metric values, points,
or pass/fail. Only failed criterion keys supplied by the scorer are accepted.
"""

import json

from app.config import get_settings
from app.dialog.llm import LlmClient, LlmRequest, LlmUnavailable, is_loopback_url
from app.domain.events import AICoaching, AIRecommendation, Metric


async def coach(metrics: list[Metric]) -> AICoaching:
    failed = [item for item in metrics if not item.passed and item.weight > 0]
    if not failed:
        return AICoaching(status="not_needed")

    settings = get_settings()
    if not settings.assessment_feedback_enabled:
        return AICoaching(status="disabled")
    if (not settings.llm_model_control or not is_loopback_url(
        settings.llm_control_base_url,
        allow_docker_host=settings.allow_docker_host_models,
    )):
        return AICoaching(status="unavailable")

    allowed = {item.key for item in failed}
    schema = {"type": "json_object", "schema": {
        "type": "object",
        "properties": {"recommendations": {
            "type": "array", "maxItems": 3,
            "items": {"type": "object",
                "properties": {
                    "metric_key": {"type": "string", "enum": sorted(allowed)},
                    "text": {"type": "string", "minLength": 12, "maxLength": 240},
                },
                "required": ["metric_key", "text"], "additionalProperties": False,
            },
        }},
        "required": ["recommendations"], "additionalProperties": False,
    }}
    evidence = [{"metric_key": item.key, "criterion": item.title,
                 "observed": item.fact, "expected": item.norm}
                for item in failed[:12]]
    request = LlmRequest(
        model=settings.llm_model_control,
        messages=[{
            "role": "system",
            "content": (
                "Ты методист учебного центра 112. По результатам детерминированной оценки "
                "сформулируй до трёх коротких, конкретных рекомендаций курсанту: что "
                "потренировать и как. Не пересчитывай баллы и не оспаривай зачёт. "
                "Опирайся только на переданные наблюдения и нормативы; не придумывай "
                "новые факты, требования и числа. Каждая рекомендация должна ссылаться "
                "на один из переданных metric_key. Верни только JSON. /no_think"
            ),
        }, {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}],
        temperature=0.0, max_tokens=360, response_format=schema, strip_reasoning=True,
    )
    client = LlmClient(base_url=settings.llm_control_base_url, timeout=6)
    try:
        raw = await client.complete(request, use_cache=True)
        payload = json.loads(raw)
        if set(payload) != {"recommendations"} or not isinstance(payload["recommendations"], list):
            raise ValueError("invalid coaching schema")
        recommendations: list[AIRecommendation] = []
        seen: set[str] = set()
        for item in payload["recommendations"]:
            if (not isinstance(item, dict) or set(item) != {"metric_key", "text"}
                    or item["metric_key"] not in allowed or item["metric_key"] in seen):
                raise ValueError("recommendation references an unscored criterion")
            recommendation = AIRecommendation.model_validate(item)
            seen.add(recommendation.metric_key)
            recommendations.append(recommendation)
        if not recommendations:
            raise ValueError("model returned no recommendations")
        return AICoaching(status="ready", model=settings.llm_model_control,
                          recommendations=recommendations)
    except (LlmUnavailable, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return AICoaching(status="unavailable")
    finally:
        await client.aclose()
