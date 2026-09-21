"""Пересчёт предварительной шкалы под утверждённый методистом сценарий."""

from app.scoring.gost import GostResult


def apply_weights(result: GostResult, overrides: dict[str, float]) -> None:
    """Меняет только веса метрик; факты, нормативы и ошибки неизменны."""
    for metric in result.metrics:
        if metric.key in overrides:
            metric.weight = overrides[metric.key]
