"""Какая метрика каким кодом ошибки и какой компетенцией размечается.

Это методика, а не вычисление: таблица читается глазами и сверяется
с docs/product/METHODOLOGY.md. Считает метрики gost.py, сюда он только смотрит.
"""

from app.domain.taxonomy import Competency, ErrorCode

#: Метрика → (код ошибки при провале, компетенция радара).
METRIC_MAP: dict[str, tuple[ErrorCode, Competency]] = {
    "answer_time": (ErrorCode.E3, Competency.INTAKE),
    "callback": (ErrorCode.E3, Competency.INTAKE),
    "checklist_completeness": (ErrorCode.E1, Competency.INTERVIEW),
    "interview_time": (ErrorCode.E3, Competency.NORMS),
    "incident_type": (ErrorCode.E2, Competency.ROUTING),
    "dds_choice": (ErrorCode.E2, Competency.ROUTING),
    "address": (ErrorCode.E5, Competency.CARD),
    "victims_count": (ErrorCode.E5, Competency.CARD),
    "required_fields": (ErrorCode.E5, Competency.CARD),
}

#: Вес детерминированного слоя в итоговой оценке. Остальное — LLM-судья
#: на мягкие критерии (E4), и не больше (docs/arch/BACKEND.md).
DETERMINISTIC_WEIGHT = 0.6
JUDGE_WEIGHT = 0.4
