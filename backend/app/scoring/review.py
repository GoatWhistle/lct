"""Разбор по отметкам: преподаватель подтверждает, снимает и добавляет отметку.

Баллы по-прежнему считает код. Преподаватель меняет входы — какие отметки
действуют, — а не формулу: балл пересчитывается теми же метриками и весами.
Связь «отметка → метрика» — методика `METRIC_MAP` в пределах карточки и службы;
если генератор знает метрику точно, она записана в `Finding.metric_key`.

Функции чистые и работают со словарём сохранённой оценки: живое занятие,
строка `Score` и архивный разбор меняются одним и тем же путём, как правка
итога в `apply_score_override`.
"""

from app.domain.events import Metric
from app.domain.taxonomy import Finding, FindingDecision, FindingReview, FindingSource
from app.scoring.competency import radar
from app.scoring.gost import GostResult
from app.scoring.taxonomy import INSTRUCTOR_METRIC, METRIC_MAP

__all__ = ["INSTRUCTOR_METRIC", "add_finding", "has_finding", "linked", "review_finding",
           "reviewed_metrics"]


def _in_scope(finding: Finding, metric: Metric) -> bool:
    if finding.card != metric.card:
        return False
    return finding.service is None or metric.service is None or finding.service == metric.service


def linked(finding: Finding, metric: Metric) -> bool:
    """Отметка объясняет провал этой метрики."""
    if not _in_scope(finding, metric):
        return False
    if finding.metric_key is not None:
        return finding.metric_key == metric.key
    mapping = METRIC_MAP.get(metric.key)
    return mapping is not None and mapping[0] is finding.code


def _dismissed(finding: Finding) -> bool:
    return finding.review is not None and finding.review.decision is FindingDecision.DISMISSED


def _credit(metric: Metric) -> float:
    return metric.credit if metric.credit is not None else float(metric.passed)


def reviewed_metrics(metrics: list[Metric], findings: list[Finding],
                     instructor_weight: float = 1.0) -> list[Metric]:
    """Метрики с учётом решений преподавателя.

    Проваленная метрика засчитывается, только если все её автоматические
    отметки сняты. Ручная отметка проваливает связанные метрики; если их нет,
    появляется отдельная метрика преподавателя. Исходный вердикт остаётся в
    `passed_auto`/`credit_auto`, повторный пересчёт начинается с него.
    """
    base = [
        metric.model_copy(update={
            "passed": metric.passed_auto if metric.passed_auto is not None else metric.passed,
            "credit": metric.credit_auto if metric.passed_auto is not None else metric.credit,
            "passed_auto": None,
            "credit_auto": None,
        })
        for metric in metrics
        if metric.key != INSTRUCTOR_METRIC
    ]
    result: list[Metric] = []
    for metric in base:
        links = [finding for finding in findings if linked(finding, metric)]
        manual = [finding for finding in links
                  if finding.source is FindingSource.INSTRUCTOR and not _dismissed(finding)]
        automatic = [finding for finding in links if finding.source is not FindingSource.INSTRUCTOR]
        if manual and _credit(metric) > 0:
            metric = metric.model_copy(update={
                "passed": False, "credit": 0.0,
                "passed_auto": metric.passed, "credit_auto": metric.credit,
            })
        elif (not manual and _credit(metric) < 1 and automatic
              and all(_dismissed(finding) for finding in automatic)):
            metric = metric.model_copy(update={
                "passed": True, "credit": 1.0,
                "passed_auto": metric.passed, "credit_auto": metric.credit,
            })
        result.append(metric)

    for finding in findings:
        if (finding.source is FindingSource.INSTRUCTOR and not _dismissed(finding)
                and not any(linked(finding, metric) for metric in base)):
            result.append(Metric(
                key=INSTRUCTOR_METRIC,
                title=f"Отметка преподавателя: {finding.summary}",
                fact=finding.fact,
                norm=finding.norm or "по решению преподавателя",
                ref=finding.ref,
                passed=False,
                weight=instructor_weight,
                card=finding.card,
                service=finding.service,
            ))
    return result


def _instructor_weight(report: dict) -> float:
    """Вес метрики преподавателя из весов занятия: веса настраивает преподаватель."""
    criteria = (report.get("full_report") or {}).get("criteria") or {}
    return float((criteria.get("score_weights") or {}).get(INSTRUCTOR_METRIC, 1.0))


def _codes(findings: list[Finding]) -> dict[str, int]:
    """Коды действующих отметок: по ним строятся частые ошибки группы и профиль."""
    codes: dict[str, int] = {}
    for finding in findings:
        if not _dismissed(finding):
            codes[finding.code.value] = codes.get(finding.code.value, 0) + 1
    return codes


def _cards(cards: list[dict], metrics: list[Metric], findings: list[Finding]) -> list[dict]:
    """Карточки очереди ДДС с тем же разбором: их метрики — часть метрик занятия."""
    updated = []
    for number, card in enumerate(cards, 1):
        own = [metric for metric in metrics if metric.card == number]
        updated.append({
            **card,
            "metrics": [metric.model_dump(mode="json") for metric in own],
            "findings": [finding.model_dump(mode="json")
                         for finding in findings if finding.card == number],
            "score_reviewed": GostResult(metrics=own).score,
        })
    return updated


def _rescore(report: dict, findings: list[Finding]) -> dict:
    metrics = reviewed_metrics(
        [Metric.model_validate(item) for item in report.get("metrics", [])], findings,
        _instructor_weight(report),
    )
    score = GostResult(metrics=metrics).score
    fields = {
        "findings": [finding.model_dump(mode="json") for finding in findings],
        "metrics": [metric.model_dump(mode="json") for metric in metrics],
        "competencies": [item.model_dump() for item in radar(metrics)],
        "score_reviewed": score,
    }
    # Правка итога — последнее слово преподавателя; решение по отметке её не отменяет.
    if not report.get("overridden_by"):
        fields["score_final"] = score
    updated = {**report, **fields}
    if "summary" in report:
        updated["summary"] = {**(report.get("summary") or {}), "codes": _codes(findings)}
    if report.get("card_results"):
        updated["card_results"] = _cards(report["card_results"], metrics, findings)
    archived = report.get("full_report")
    if isinstance(archived, dict):
        failed = sum(not metric.passed and metric.weight > 0 for metric in metrics)
        allowed = (archived.get("criteria") or {}).get("allowed_errors", 0)
        updated["full_report"] = {
            **archived, **fields,
            "card_results": _cards(archived.get("card_results") or [], metrics, findings),
            "failed_metrics": failed,
            "passed": failed <= allowed,
            "score_final": updated.get("score_final", archived.get("score_final")),
        }
    return updated


def _findings(report: dict) -> list[Finding]:
    return [Finding.model_validate(item) for item in report.get("findings", [])]


def review_finding(report: dict, index: int, review: FindingReview) -> dict:
    """Подтвердить или снять отметку. Решение дописывается, действует последнее."""
    findings = _findings(report)
    if not 0 <= index < len(findings):
        raise LookupError(f"нет отметки {index}")
    findings[index] = findings[index].model_copy(
        update={"reviews": [*findings[index].reviews, review]})
    return _rescore(report, findings)


def has_finding(report: dict, client_id) -> bool:
    """Ручная отметка с этим ключом уже в разборе — повтор запроса."""
    return client_id is not None and any(
        finding.client_id == client_id for finding in _findings(report))


def add_finding(report: dict, finding: Finding) -> dict:
    """Добавить отметку преподавателя; она штрафует связанную метрику."""
    if finding.source is not FindingSource.INSTRUCTOR:
        raise ValueError("вручную добавляется только отметка преподавателя")
    # Повтор (ретрай сети, вторая вкладка) не штрафует второй раз.
    if has_finding(report, finding.client_id):
        return report
    return _rescore(report, [*_findings(report), finding])
