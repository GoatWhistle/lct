"""Временная составляющая оценки по занятой методике.

Скорость даёт непрерывный вклад, а превышение норматива дополнительно
отмечается как E3. Линейный множитель 1 − t/(2T) ограничен диапазоном 0–1:
нулевое время даёт полный вклад, два норматива и более — нулевой.
"""

from dataclasses import dataclass

from app.domain.events import Metric
from app.domain.taxonomy import Competency, ErrorCode, Finding, FindingSource
from app.scoring.taxonomy import METRIC_WEIGHTS

TIME_REF = "ТЗ задачи № 9; сессия вопросов и ответов 16.09.2026"


def time_credit(elapsed_ms: int | None, limit_ms: int) -> float:
    """Доля веса временной метрики, 0–1; отсутствие доказанного времени — 0."""
    if elapsed_ms is None or elapsed_ms < 0 or limit_ms <= 0:
        return 0.0
    return round(max(0.0, min(1.0, 1.0 - elapsed_ms / (2 * limit_ms))), 4)


@dataclass(frozen=True)
class TimeNorm:
    """Формулировки норматива времени: карточка 112 и карточка ДДС различаются
    только словами, расчёт и отметка E3 у них общие."""

    key: str
    title: str
    summary: str
    action: str
    unmeasured: str
    incomplete: str


CARD_FILL_TIME = TimeNorm(
    key="card_fill_time",
    title="Время заполнения карточки",
    summary="Время заполнения карточки: норматив не выполнен",
    action="сдать карточку",
    unmeasured="время не зафиксировано",
    incomplete="карточка не сдана",
)
DDS_WORK_TIME = TimeNorm(
    key="dds_work_time",
    title="Первая запись по карточке ДДС",
    summary="ДДС: первая запись по карточке позже норматива",
    action="открыть карточку и добавить первую запись (статус и текст)",
    unmeasured="карточка не открыта",
    incomplete="первая запись не добавлена",
)


def time_metric(
    norm: TimeNorm, elapsed_ms: int | None, limit_ms: int, *, completed: bool
) -> tuple[Metric, Finding | None]:
    """Метрика норматива и отметка E3, если он не выполнен.

    Незавершённая работа не получает временного вклада: быстрое, но брошенное
    заполнение не должно выглядеть скоростью.
    """
    if elapsed_ms is None:
        fact = norm.unmeasured
    else:
        delta_ms = elapsed_ms - limit_ms
        if delta_ms > 0:
            deviation = f"+{round(delta_ms / 1000)} с сверх норматива"
        elif delta_ms < 0:
            deviation = f"на {round(abs(delta_ms) / 1000)} с быстрее норматива"
        else:
            deviation = "точно в норматив"
        fact = f"{round(elapsed_ms / 1000)} с ({deviation})"
    if not completed:
        fact = f"{norm.incomplete}; {fact}"
    norm_text = f"{norm.action} за {round(limit_ms / 1000)} с"
    passed = completed and elapsed_ms is not None and elapsed_ms <= limit_ms
    metric = Metric(
        key=norm.key,
        title=norm.title,
        fact=fact,
        norm=norm_text,
        ref=TIME_REF,
        passed=passed,
        weight=METRIC_WEIGHTS[norm.key],
        credit=time_credit(elapsed_ms, limit_ms) if completed else 0.0,
    )
    if passed:
        return metric, None
    return metric, Finding(
        code=ErrorCode.E3,
        source=FindingSource.TIMERS,
        summary=norm.summary,
        fact=fact,
        norm=norm_text,
        ref=TIME_REF,
        competency=Competency.NORMS,
    )
