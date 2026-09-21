"""Веса метрик настраиваются сценарием и не меняют факт проверки."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.events import Metric
from app.scenarios.loader import load_file
from app.scenarios.schema import Scenario
from app.scoring.gost import GostResult
from app.scoring.weights import apply_weights


LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


def test_weight_override_changes_score_but_not_findings():
    result = GostResult(metrics=[
        Metric(key="address", title="Адрес", fact="пусто", norm="заполнен", passed=False, weight=2),
        Metric(key="required_fields", title="Поля", fact="есть", norm="есть", passed=True, weight=2),
    ])
    assert result.score == 50.0
    apply_weights(result, {"address": 6.0})
    assert result.score == 25.0
    assert result.metrics[0].fact == "пусто"


def test_scenario_accepts_only_known_finite_weights():
    source = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    body = source.model_dump(mode="json")
    body["score_weights"] = {"address": 3.0, "dds_ack": 0.0}
    assert Scenario.model_validate(body).score_weights["address"] == 3.0
    body["score_weights"] = {"typo": 3.0}
    with pytest.raises(ValidationError, match="неизвестные метрики"):
        Scenario.model_validate(body)
    body["score_weights"] = {"address": 11.0}
    with pytest.raises(ValidationError, match="от 0 до 10"):
        Scenario.model_validate(body)
