"""Детерминированный слой оценки — 60% веса, считается кодом.

Воспроизводится стопроцентно: один и тот же ход занятия даёт один и тот же
результат. Каждая метрика — «факт против норматива со ссылкой», а не балл:
«опрос 94 с при нормативе 75 с (ГОСТ Р 22.7.03-2021)» можно предъявить
и проверить руками (docs/product/DEBRIEF.md).
"""

import re
from dataclasses import dataclass, field

from app.domain.events import CallEndReason, Metric
from app.domain.kio import KIO, missing_fields
from app.domain.taxonomy import ERRORS, Competency, Finding, FindingSource
from app.domain.timers import GOST_REF, NORMATIVES, TimerCode
from app.scenarios.schema import Scenario
from app.scoring.taxonomy import METRIC_MAP, METRIC_WEIGHTS
from app.session.timers import SessionTimers

SOURCE_BY_CODE = {
    "E1": FindingSource.SLOTS,
    "E2": FindingSource.GROUND_TRUTH,
    "E3": FindingSource.TIMERS,
    "E5": FindingSource.KIO,
}


@dataclass
class GostResult:
    metrics: list[Metric] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: Метрики, которые посчитать было нечем. Не штрафуют, но видны в отчёте:
    #: молча выброшенная метрика выглядит как пройденная.
    unavailable: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Доля пройденного веса, 0–100."""
        total = sum(metric.weight for metric in self.metrics)
        if not total:
            return 0.0
        passed = sum(metric.weight for metric in self.metrics if metric.passed)
        return round(100 * passed / total, 1)


def _seconds(ms: int) -> str:
    return f"{round(ms / 1000)} с"


def _normalize_address(text: str | None) -> set[str]:
    """Слова адреса без служебных: «ул. Ленина д. 14» и «улица Ленина, 14»
    должны совпасть, иначе курсанта штрафуют за сокращение."""
    if not text:
        return set()
    noise = {"улица", "ул", "дом", "д", "проспект", "пр", "переулок", "пер", "г", "город", "москва"}
    words = re.findall(r"[\w-]+", text.lower().replace("ё", "е"))
    return {word for word in words if word not in noise}


class _Builder:
    def __init__(self) -> None:
        self.result = GostResult()

    def add(self, key: str, title: str, fact: str, norm: str, passed: bool, ref: str | None = None,
            finding: str | None = None) -> None:
        self.result.metrics.append(
            Metric(key=key, title=title, fact=fact, norm=norm, ref=ref, passed=passed,
                   weight=METRIC_WEIGHTS.get(key, 1.0))
        )
        if passed:
            return
        code, competency = METRIC_MAP[key]
        self.result.findings.append(
            Finding(
                code=code,
                source=SOURCE_BY_CODE[code.value],
                summary=finding or f"{ERRORS[code].title}: {title.lower()}",
                fact=fact,
                norm=norm,
                ref=ref,
                competency=competency,
            )
        )

    def timer(self, key: str, code: TimerCode, timers: SessionTimers, limit_ms: int,
              not_stopped: str) -> None:
        normative = NORMATIVES[code]
        norm = f"≤ {_seconds(limit_ms)}"
        measured = timers.measured_ms(code)
        if measured is None:
            # Таймер не остановлен событием — время недоказуемо, и это провал:
            # норматив не выполнен, пока операция не завершена.
            self.add(key, normative.title, not_stopped, norm, passed=False, ref=GOST_REF)
            return
        self.add(
            key,
            normative.title,
            f"{_seconds(measured)}",
            norm,
            passed=measured <= limit_ms,
            ref=GOST_REF,
            finding=f"{normative.title}: {_seconds(measured)} при нормативе {_seconds(limit_ms)}",
        )


def evaluate(
    *,
    scenario: Scenario,
    kio: KIO,
    timers: SessionTimers,
    revealed_facts: list[str] | None,
    end_reason: CallEndReason | None = None,
) -> GostResult:
    """Посчитать детерминированный слой по завершённому занятию.

    `revealed_facts` — из слот-автомата. None означает, что автомата не было
    (нет модели эмбеддингов): полнота опроса тогда не считается и не штрафует.
    """
    build = _Builder()
    truth = scenario.ground_truth

    # ── нормативы времени, E3 ──
    build.timer("answer_time", TimerCode.ANSWER, timers, timers.limits[TimerCode.ANSWER],
                "вызов не принят")
    build.timer("interview_time", TimerCode.INTERVIEW, timers, timers.limits[TimerCode.INTERVIEW],
                "опрос не завершён передачей в ДДС — время не зафиксировано")
    build.result.unavailable.append(
        "dds_notify_time: нет события конца опроса, от которого отсчитывать 60 с "
        "(см. docs/arch/CONTRACT.md, коды таймеров)"
    )

    if end_reason is CallEndReason.DROPPED:
        callback = timers.timers.get(TimerCode.CALLBACK)
        attempts = callback.attempt if callback and callback.started_at is not None else 0
        limit = NORMATIVES[TimerCode.CALLBACK]
        build.add(
            "callback", limit.title,
            f"попыток дозвона: {attempts}" if attempts else "обратного дозвона не было",
            f"не более {limit.attempts} попыток по {_seconds(limit.limit_ms)}",
            passed=0 < attempts <= limit.attempts,
            ref=GOST_REF,
        )

    # ── полнота опроса, E1 ──
    if revealed_facts is None:
        build.result.unavailable.append(
            "checklist_completeness: нет слот-автомата (не скачана модель эмбеддингов)"
        )
    else:
        required = truth.required_facts
        got = [fact_id for fact_id in required if fact_id in revealed_facts]
        build.result.metrics.append(
            Metric(
                key="checklist_completeness",
                title="Полнота опроса",
                fact=f"добыто {len(got)} из {len(required)} обязательных фактов",
                norm="все обязательные факты",
                ref="чек-лист сценария",
                passed=len(got) == len(required),
                weight=METRIC_WEIGHTS["checklist_completeness"],
            )
        )
        # По отметке на каждый недобытый факт: в разборе нужен конкретный
        # пропущенный вопрос, а не процент.
        questions = {item.fact: item.question for item in scenario.checklist if item.fact}
        for fact_id in required:
            if fact_id in revealed_facts:
                continue
            question = questions.get(fact_id)
            build.result.findings.append(
                Finding(
                    code=METRIC_MAP["checklist_completeness"][0],
                    source=FindingSource.SLOTS,
                    summary=f"Не добыт обязательный факт {fact_id}",
                    fact="вопрос не прозвучал",
                    norm=f"эталонный вопрос: «{question}»" if question else "обязательный факт сценария",
                    ref="чек-лист сценария",
                    competency=Competency.INTERVIEW,
                )
            )

    # ── классификация и маршрутизация, E2 ──
    if truth.incident_type is not None:
        actual = kio.incident_type.value if kio.incident_type else "не указан"
        build.add(
            "incident_type", "Тип происшествия",
            actual, truth.incident_type.value,
            passed=kio.incident_type == truth.incident_type,
            ref="классификатор происшествий",
            finding=f"Тип происшествия {actual}, верный — {truth.incident_type.value}",
        )
    if truth.dds is not None:
        actual = kio.dds.value if kio.dds else "не выбрана"
        build.add(
            "dds_choice", "Выбор ДДС",
            actual, truth.dds.value,
            passed=kio.dds == truth.dds,
            ref="классификатор ДДС",
            finding=f"Карточка ушла в ДДС {actual}, верная — {truth.dds.value}",
        )

    # ── карточка, E5 ──
    if truth.address:
        written = kio.address or " ".join(filter(None, [kio.street, kio.building]))
        expected = _normalize_address(truth.address)
        build.add(
            "address", "Адрес",
            written or "не заполнен", truth.address,
            passed=bool(expected) and expected <= _normalize_address(written),
            ref="ground_truth сценария",
            finding=f"Адрес в карточке «{written or 'пусто'}», верный — «{truth.address}»",
        )
    if truth.victims is not None:
        actual = "не указано" if kio.victims_count is None else str(kio.victims_count)
        build.add(
            "victims_count", "Число пострадавших",
            actual, str(truth.victims),
            passed=kio.victims_count == truth.victims,
            ref="ground_truth сценария",
            finding=f"Пострадавших в карточке {actual}, верно — {truth.victims}",
        )
    if scenario.required_fields:
        empty = missing_fields(kio, scenario.required_fields)
        build.add(
            "required_fields", "Обязательные поля КИО",
            "все заполнены" if not empty else f"пусто: {', '.join(empty)}",
            f"заполнены: {', '.join(scenario.required_fields)}",
            passed=not empty,
            ref="ГОСТ Р 22.7.03-2021, структура КИО",
            finding=f"Не заполнены обязательные поля: {', '.join(empty)}" if empty else None,
        )

    return build.result
