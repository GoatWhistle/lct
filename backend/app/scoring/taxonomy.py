"""Какая метрика каким кодом ошибки и какой компетенцией размечается.

Это методика, а не вычисление: таблица читается глазами и сверяется
с docs/product/METHODOLOGY.md. Считает метрики gost.py, сюда он только смотрит.
"""

from app.domain.taxonomy import Competency, ErrorCode

#: Метрика → (код ошибки при провале, компетенция радара).
METRIC_MAP: dict[str, tuple[ErrorCode, Competency]] = {
    "answer_time": (ErrorCode.E3, Competency.INTAKE),
    "dds_chain": (ErrorCode.E6, Competency.CARD),
    "callback": (ErrorCode.E3, Competency.INTAKE),
    "checklist_completeness": (ErrorCode.E1, Competency.INTERVIEW),
    "address_refined": (ErrorCode.E1, Competency.INTERVIEW),
    "interview_time": (ErrorCode.E3, Competency.NORMS),
    "outcome": (ErrorCode.E2, Competency.ROUTING),
    "incident_signs": (ErrorCode.E2, Competency.ROUTING),
    "incident_type": (ErrorCode.E2, Competency.ROUTING),
    "dds_choice": (ErrorCode.E2, Competency.ROUTING),
    "address": (ErrorCode.E5, Competency.CARD),
    "victims_count": (ErrorCode.E5, Competency.CARD),
    "required_fields": (ErrorCode.E5, Competency.CARD),
    "dds_primary": (ErrorCode.D1, Competency.CARD),
    "dds_ack": (ErrorCode.D1, Competency.NORMS),
    "dds_decision": (ErrorCode.D3, Competency.ROUTING),
    "dds_crew": (ErrorCode.D2, Competency.ROUTING),
    "dds_contact": (ErrorCode.D6, Competency.COMMUNICATION),
    "dds_progress": (ErrorCode.D6, Competency.CARD),
    "dds_completion": (ErrorCode.D6, Competency.CARD),
    "dds_reply": (ErrorCode.D5, Competency.COMMUNICATION),
}

#: Вес метрики в детерминированной оценке.
#:
#: **Предварительные значения, требуют утверждения методистом.** Без весов все
#: метрики равны, и курсант, не задавший ни одного вопроса, но заполнивший
#: карточку руками, получает 87 из 100: «ответ за секунду» стоит столько же,
#: сколько «добыл все обязательные факты». Опрос — то, ради чего существует
#: тренажёр, поэтому он весит больше всего.
METRIC_WEIGHTS: dict[str, float] = {
    "checklist_completeness": 4.0,
    "address_refined": 2.0,
    "outcome": 3.0,
    "incident_signs": 2.0,
    "incident_type": 2.0,
    "dds_choice": 2.0,
    "address": 2.0,
    "required_fields": 2.0,
    "interview_time": 1.5,
    "victims_count": 1.0,
    "answer_time": 1.0,
    "callback": 1.0,
    "dds_chain": 2.0,
}

#: Вес детерминированного слоя в итоговой оценке. Остальное — LLM-судья
#: на мягкие критерии (E4), и не больше (docs/arch/BACKEND.md).
DETERMINISTIC_WEIGHT = 0.6
JUDGE_WEIGHT = 0.4
