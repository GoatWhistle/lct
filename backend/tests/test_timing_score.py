import pytest

from app.domain.events import Metric
from app.domain.taxonomy import Competency, ErrorCode, FindingSource
from app.scoring.competency import radar
from app.scoring.gost import GostResult
from app.scoring.timing import CARD_FILL_TIME, DDS_WORK_TIME, time_credit, time_metric


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


@pytest.mark.parametrize(
    ("elapsed_ms", "completed", "fact", "passed"),
    [
        (200_000, True, "200 с (+20 с сверх норматива)", False),
        (61_000, True, "61 с (на 119 с быстрее норматива)", True),
        (180_000, True, "180 с (точно в норматив)", True),
        (None, True, "время не зафиксировано", False),
        (30_000, False, "карточка не сдана; 30 с (на 150 с быстрее норматива)", False),
    ],
)
def test_card_fill_time_metric_texts(elapsed_ms, completed, fact, passed):
    metric, finding = time_metric(CARD_FILL_TIME, elapsed_ms, 180_000, completed=completed)

    assert metric.key == "card_fill_time"
    assert metric.title == "Время заполнения карточки"
    assert metric.fact == fact
    assert metric.norm == "сдать карточку за 180 с"
    assert metric.ref == "ТЗ задачи № 9; сессия вопросов и ответов 16.09.2026"
    assert metric.passed is passed
    assert metric.credit == (time_credit(elapsed_ms, 180_000) if completed else 0.0)
    if passed:
        assert finding is None
    else:
        assert finding.code is ErrorCode.E3
        assert finding.source is FindingSource.TIMERS
        assert finding.competency is Competency.NORMS
        assert finding.summary == "Время заполнения карточки: норматив не выполнен"
        assert (finding.fact, finding.norm) == (fact, "сдать карточку за 180 с")


@pytest.mark.parametrize(
    ("elapsed_ms", "completed", "fact", "passed"),
    [
        (605_000, True, "605 с (+425 с сверх норматива)", False),
        (25_000, True, "25 с (на 155 с быстрее норматива)", True),
        (180_000, True, "180 с (точно в норматив)", True),
        (None, False, "первая запись не добавлена; карточка не открыта", False),
        (25_000, False, "первая запись не добавлена; 25 с (на 155 с быстрее норматива)", False),
    ],
)
def test_dds_work_time_metric_texts(elapsed_ms, completed, fact, passed):
    metric, finding = time_metric(DDS_WORK_TIME, elapsed_ms, 180_000, completed=completed)

    assert metric.key == "dds_work_time"
    assert metric.title == "Первая запись по карточке ДДС"
    assert metric.fact == fact
    assert metric.norm == "открыть карточку и добавить первую запись (статус и текст) за 180 с"
    assert metric.passed is passed
    assert (finding is None) is passed
    if finding is not None:
        assert finding.summary == "ДДС: первая запись по карточке позже норматива"
        assert (finding.fact, finding.norm) == (
            fact, "открыть карточку и добавить первую запись (статус и текст) за 180 с"
        )
