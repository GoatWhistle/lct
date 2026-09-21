"""Воспроизводимая аналитика учебной группы по сохранённым оценкам.

Рекомендации здесь основаны на кодах ошибок, а не выдаются за вывод ИИ.
Повторные попытки учитываются в среднем балле, но один курсант не может
увеличить долю группы повторением одной и той же ошибки.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from uuid import UUID

from app.domain.taxonomy import ERRORS, ErrorCode


RECOMMENDATIONS: dict[str, str] = {
    "E1": "Повторить опросную карту и обязательные уточняющие вопросы.",
    "E2": "Отработать классификацию происшествия и маршрутизацию по ЕКП.",
    "E3": "Провести тренировку по временным нормативам.",
    "E4": "Разобрать управление разговором и коммуникацию в стрессе.",
    "E5": "Потренировать полноту заполнения карточки 112.",
    "E6": "Повторить проверку карточки перед завершением и передачей.",
    "D1": "Отработать первичную отметку ДДС в течение 30 секунд.",
    "D2": "Сверять статусы реагирования с фактическими докладами.",
    "D3": "Разобрать профильность обращений и допустимость отказа.",
    "D4": "Тренировать обоснование отказа в комментарии.",
    "D5": "Тренировать полноту комментария: действие и передача информации.",
    "D6": "Повторить цепочку статусов выезда, прибытия и работ.",
}


@dataclass(frozen=True)
class ScoredAttempt:
    trainee_id: UUID | None
    score: float
    codes: dict[str, int]


def summarize(attempts: list[ScoredAttempt], enrolled: int) -> dict:
    """Сводка только по завершённым попыткам с оценкой."""
    active = {item.trainee_id for item in attempts if item.trainee_id is not None}
    affected: dict[str, set[UUID]] = defaultdict(set)
    occurrences: Counter[str] = Counter()
    for attempt in attempts:
        for code, count in attempt.codes.items():
            if code not in RECOMMENDATIONS or not isinstance(count, int) or count <= 0:
                continue
            occurrences[code] += count
            if attempt.trainee_id is not None:
                affected[code].add(attempt.trainee_id)

    errors = []
    for code in occurrences:
        errors.append({
            "code": code,
            "title": ERRORS[ErrorCode(code)].title,
            "affected_trainees": len(affected[code]),
            "occurrences": occurrences[code],
            "rate_percent": round(100 * len(affected[code]) / len(active), 1) if active else 0.0,
            "recommendation": RECOMMENDATIONS[code],
        })
    errors.sort(key=lambda item: (-item["affected_trainees"], -item["occurrences"], item["code"]))
    return {
        "enrolled_trainees": enrolled,
        "active_trainees": len(active),
        "scored_attempts": len(attempts),
        "average_score": round(sum(item.score for item in attempts) / len(attempts), 1)
        if attempts else None,
        "errors": errors,
    }
