"""Радар шести компетенций.

Чистая проекция уже посчитанных метрик, без пересчёта весов: радар и оценка
всегда согласованы, и расхождение между ними невозможно по построению
(docs/product/METHODOLOGY.md#компетентная-модель).
"""

from app.domain.events import CompetencyScore, Metric
from app.domain.taxonomy import Competency
from app.scoring.taxonomy import METRIC_MAP


def radar(metrics: list[Metric]) -> list[CompetencyScore]:
    """Доля пройденного веса по каждой компетенции, 0–1.

    Компетенция без метрик не рисуется нулём: «коммуникация» без судьи —
    это «не оценивалось», а не «провалено».
    """
    total: dict[Competency, float] = {}
    passed: dict[Competency, float] = {}
    for metric in metrics:
        mapping = METRIC_MAP.get(metric.key)
        if mapping is None:
            continue
        competency = mapping[1]
        total[competency] = total.get(competency, 0.0) + metric.weight
        if metric.passed:
            passed[competency] = passed.get(competency, 0.0) + metric.weight

    return [
        CompetencyScore(competency=competency.value, value=round(passed.get(competency, 0.0) / weight, 3))
        for competency in Competency
        if (weight := total.get(competency))
    ]
