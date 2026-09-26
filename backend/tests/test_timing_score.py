import pytest

from app.domain.events import Metric
from app.scoring.competency import radar
from app.scoring.gost import GostResult
from app.scoring.timing import time_credit


@pytest.mark.parametrize(
    ("elapsed_ms", "expected"),
    [(0, 1.0), (90_000, 0.75), (180_000, 0.5), (360_000, 0.0), (400_000, 0.0),
     (None, 0.0)],
)
def test_time_credit_reduces_linearly_against_the_norm(elapsed_ms, expected):
    assert time_credit(elapsed_ms, 180_000) == expected


def test_fractional_timing_credit_affects_total_and_competency_scores():
    metric = Metric(
        key="card_fill_time", title="Время заполнения карточки", fact="90 с",
        norm="180 с", passed=True, weight=2.0, credit=0.75,
    )
    result = GostResult(metrics=[metric])
    assert result.score == 75.0
    assert radar([metric])[0].value == 0.75
