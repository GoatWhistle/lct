"""Метрики опроса и КИО — объяснимые правила, считаются кодом.

Воспроизводится стопроцентно: один и тот же ход занятия даёт один и тот же
результат. Каждая метрика — «факт против норматива со ссылкой». Для временных
метрик к двоичному признаку нарушения добавлен прозрачный непрерывный вклад,
описанный в docs/product/METHODOLOGY.md.
"""

from dataclasses import dataclass, field

from app.domain import ekp
from app.domain.classifiers import OUTCOME_LABELS, Outcome
from app.domain.events import CallEndReason, Metric
from app.domain.kio import KIO, missing_fields
from app.domain.taxonomy import ERRORS, Competency, Finding, FindingSource
from app.domain.timers import GOST_REF, NORMATIVES, TimerCode
from app.scenarios.schema import Scenario
from app.scoring.address import address_matches
from app.scoring.taxonomy import METRIC_MAP, METRIC_WEIGHTS
from app.session.timers import SessionTimers

SOURCE_BY_CODE = {
    "E6": FindingSource.CHAIN,
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
        passed = sum(metric.weight * (metric.credit if metric.credit is not None
                                      else float(metric.passed)) for metric in self.metrics)
        return round(100 * passed / total, 1)


def _seconds(ms: int) -> str:
    return f"{round(ms / 1000)} с"


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
                metric_key=key,
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
    refined_facts: list[str] | None = None,
    resolved_outcome: str | None = None,
    dispatched: bool = False,
    end_reason: CallEndReason | None = None,
    bounced_fields: list[str] | None = None,
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
                    metric_key="checklist_completeness",
                )
            )

    # ── классификация и маршрутизация, E2 ──
    #
    # Признаки — то, что оператор действительно делает в боевом АРМ-112: службу
    # он не выбирает, её считает система по ЕКП (docs/spec/DATASET.md). Поэтому
    # размеченный признаками сценарий оценивается по ним, а не по выбору ДДС.
    if truth.incident_code is not None:
        found = ekp.incident(truth.incident_code)
        expected = " · ".join(found.signs) if found else truth.incident_code
        actual = " · ".join(kio.signs) if kio.signs else "не проставлены"
        build.add(
            "incident_signs", "Признаки происшествия",
            actual, expected,
            passed=kio.incident_code == truth.incident_code,
            ref=f"классификатор ЕКП {ekp.reference().version}",
            finding=(
                f"Признаки «{actual}» дают {kio.incident_code or 'пустой код'}, "
                f"верные — «{expected}» ({truth.incident_code})"
            ),
        )
        # Список оповещения производен: при верных признаках он верен всегда,
        # и штрафовать за него отдельно — штрафовать дважды за одно.
    elif truth.incident_type is not None:
        actual = kio.incident_type.value if kio.incident_type else "не указан"
        build.add(
            "incident_type", "Тип происшествия",
            actual, truth.incident_type.value,
            passed=kio.incident_type == truth.incident_type,
            ref="классификатор происшествий",
            finding=f"Тип происшествия {actual}, верный — {truth.incident_type.value}",
        )
    if truth.dds is not None and truth.incident_code is None:
        actual = kio.dds.value if kio.dds else "не выбрана"
        build.add(
            "dds_choice", "Выбор ДДС",
            actual, truth.dds.value,
            passed=kio.dds == truth.dds,
            ref="классификатор ДДС",
            finding=f"Карточка ушла в ДДС {actual}, верная — {truth.dds.value}",
        )

    # ── исход вызова, E2 ──
    #
    # Не каждый звонок заканчивается карточкой: «поругался с продавцом» — справка,
    # вызов из другого субъекта передаётся туда (docs/spec/TICKETS.md). Курсант,
    # заведший карточку на такой вызов, сделал лишнюю работу и занял расчёт.
    expected_outcome = scenario.outcome
    if expected_outcome is not Outcome.CARD:
        actual = resolved_outcome or ("card" if dispatched else "ничего не решено")
        build.add(
            "outcome", "Исход вызова",
            OUTCOME_LABELS.get(Outcome(actual), actual) if actual in set(Outcome) else actual,
            OUTCOME_LABELS[expected_outcome],
            passed=actual == expected_outcome.value,
            ref="классификатор происшествий, группа 23 «Прочие происшествия»",
            finding=(
                f"Вызов закрыт как «{actual}», правильно — "
                f"«{OUTCOME_LABELS[expected_outcome]}»"
            ),
        )

    # ── подтверждение места происшествия, E1 ──
    #
    # Адрес заявителя не всегда адрес происшествия: он называет тот, где стоит
    # сам, или ориентир вместо дома. В билетах заказчика это основной приём
    # усложнения (docs/spec/TICKETS.md), и оператор обязан переспросить.
    refinable = [fact.id for fact in scenario.facts if fact.refine_on]
    if refinable and refined_facts is not None:
        confirmed = [fact_id for fact_id in refinable if fact_id in refined_facts]
        build.add(
            "address_refined", "Место происшествия подтверждено",
            f"уточнено {len(confirmed)} из {len(refinable)}", "уточнены все",
            passed=len(confirmed) == len(refinable),
            ref="памятка АРМ-112: адрес заявителя не всегда адрес происшествия",
            finding="Оператор не переспросил про место происшествия — записан адрес,"
                    " который назвал заявитель",
        )

    # ── карточка, E5 ──
    #
    # Там, где карточка не заводится, пустые поля — не ошибка, а правильное
    # поведение: штрафовать за них значит учить заполнять лишнее.
    if expected_outcome is not Outcome.CARD:
        return build.result
    if truth.address:
        written = kio.address or " ".join(filter(None, [kio.street, kio.building]))
        build.add(
            "address", "Адрес",
            written or "не заполнен", truth.address,
            passed=address_matches(truth.address, written),
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

    # ── цепочка 112 → ДДС, E6 ──
    if bounced_fields:
        build.add(
            "dds_chain", "Карточка принята ДДС",
            f"возвращена на уточнение: {', '.join(bounced_fields)}",
            "карточка пригодна для выезда",
            passed=False,
            ref="цепочка 112 → ДДС",
            finding=f"Диспетчер вернул карточку: не заполнено {', '.join(bounced_fields)} — выезд сорван",
        )

    return build.result
