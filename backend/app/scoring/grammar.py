"""Проверка письменного ответа ДДС: локальные правила и необязательный Vikhr."""

import json
import re
from dataclasses import dataclass

from app.config import get_settings
from app.dialog.llm import LlmClient, LlmRequest, LlmUnavailable, is_loopback_url


@dataclass(frozen=True)
class GrammarAssessment:
    passed: bool
    errors: tuple[str, ...]
    source: str


def basic_check(text: str) -> GrammarAssessment:
    """Воспроизводимый минимум работает даже без модели."""
    value = text.strip()
    errors: list[str] = []
    if not value:
        errors.append("ответ отсутствует")
    else:
        first_letter = next((char for char in value if char.isalpha()), "")
        if first_letter and first_letter != first_letter.upper():
            errors.append("предложение начинается со строчной буквы")
        if value[-1:] not in ".!?…":
            errors.append("нет завершающего знака препинания")
        if re.search(r"\s{2,}", value):
            errors.append("повторяющиеся пробелы")
        if re.search(r"[A-Za-z]", value):
            errors.append("латинские буквы в русском ответе")
        if re.search(r"([!?.,])\1{2,}", value):
            errors.append("избыточно повторяющиеся знаки препинания")
        agreement_errors = (
            (r"\bсообщение\s+принят(?:ы|а)\b", "нарушено согласование: «сообщение принято»"),
            (r"\bбригада\s+направлен(?:о|ы)\b", "нарушено согласование: «бригада направлена»"),
            (r"\bинформация\s+передан(?:о|ы)\b", "нарушено согласование: «информация передана»"),
            (r"\bработы\s+завершен(?:а|о)\b", "нарушено согласование: «работы завершены»"),
        )
        for pattern, message in agreement_errors:
            if re.search(pattern, value, re.IGNORECASE):
                errors.append(message)
    return GrammarAssessment(not errors, tuple(errors), "rules")


async def assess(text: str) -> GrammarAssessment:
    """Vikhr проверяет мягкую грамматику, правила остаются страховочной сеткой."""
    baseline = basic_check(text)
    settings = get_settings()
    if (not settings.grammar_llm_enabled or not text.strip()
            or not is_loopback_url(
                settings.llm_control_base_url,
                allow_docker_host=settings.allow_docker_host_models,
            )
            or not settings.llm_model_control):
        return baseline

    schema = {"type": "json_object", "schema": {
        "type": "object",
        "properties": {
            "passed": {"type": "boolean"},
            "comment": {"type": "string"},
        },
        "required": ["passed", "comment"],
        "additionalProperties": False,
    }}
    request = LlmRequest(
        messages=[{
            "role": "system",
            "content": (
                "Ты строгий корректор русского языка. Проверяй только грамматику, "
                "орфографию и пунктуацию короткого служебного сообщения; не оценивай "
                "факты и действия. Пример ошибки: «Сообщение приняты» => passed=false, "
                "comment=«Нарушено согласование: сообщение принято». Пример нормы: "
                "«Сообщение принято, бригада направлена.» => passed=true, comment=«». "
                "Найди даже одну языковую ошибку и верни JSON по заданной схеме."
            ),
        }, {"role": "user", "content": text}],
        model=settings.llm_model_control,
        temperature=0.0,
        max_tokens=160,
        response_format=schema,
    )
    client = LlmClient(base_url=settings.llm_control_base_url, timeout=5)
    try:
        raw = await client.complete(request, use_cache=True)
        payload = json.loads(raw)
        if set(payload) != {"passed", "comment"} or not isinstance(payload["passed"], bool):
            raise ValueError("неверная схема")
        comment = payload["comment"]
        if not isinstance(comment, str) or len(comment) > 500:
            raise ValueError("неверный комментарий")
        errors = list(baseline.errors)
        if not payload["passed"]:
            errors.append(comment.strip() or "локальная модель обнаружила языковые ошибки")
        return GrammarAssessment(not errors, tuple(dict.fromkeys(errors)), "vikhr")
    except (LlmUnavailable, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return baseline
    finally:
        await client.aclose()
