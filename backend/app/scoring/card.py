"""Детерминированная оценка текстового упражнения оператора 112.

Нет входящего звонка и опроса: оцениваются только поля готовой КИО и
маршрутизация. Эталон берётся из утверждённого сценария, но не показывается
в `card.briefing` до завершения упражнения.
"""

from app.domain.events import Metric
from app.domain.kio import KIO, missing_fields
from app.domain.taxonomy import Finding, FindingSource
from app.scenarios.schema import Scenario
from app.scoring.gost import GostResult
from app.scoring.address import address_matches
from app.scoring.taxonomy import METRIC_MAP, METRIC_WEIGHTS


def evaluate_card(scenario: Scenario, kio: KIO) -> GostResult:
    result = GostResult()
    truth = scenario.ground_truth

    def add(key: str, title: str, fact: str, norm: str, passed: bool, ref: str) -> None:
        result.metrics.append(Metric(
            key=key, title=title, fact=fact, norm=norm, passed=passed,
            ref=ref, weight=METRIC_WEIGHTS.get(key, 1.0),
        ))
        if not passed:
            code, competency = METRIC_MAP[key]
            result.findings.append(Finding(
                code=code,
                source=FindingSource.KIO if code.value == "E5" else FindingSource.GROUND_TRUTH,
                summary=f"{title}: {fact}; требуется {norm}",
                fact=fact, norm=norm, ref=ref, competency=competency,
                metric_key=key,
            ))

    if truth.incident_code:
        actual = kio.incident_code or "не определён"
        add("incident_signs", "Признаки происшествия", actual,
            truth.incident_code, actual == truth.incident_code, "классификатор ЕКП")
    elif truth.incident_type:
        actual = kio.incident_type.value if kio.incident_type else "не указан"
        add("incident_type", "Тип происшествия", actual,
            truth.incident_type.value, kio.incident_type == truth.incident_type,
            "классификатор происшествий")

    if truth.dds and truth.incident_code is None:
        actual = kio.dds.value if kio.dds else "не выбрана"
        add("dds_choice", "ДДС", actual, truth.dds.value,
            kio.dds == truth.dds, "список оповещения по сценарию")

    if truth.address:
        written = kio.address or " ".join(filter(None, (kio.street, kio.building)))
        add("address", "Адрес происшествия", written or "не заполнен", truth.address,
            address_matches(truth.address, written),
            "эталон сценария")

    if truth.victims is not None:
        actual = str(kio.victims_count) if kio.victims_count is not None else "не указано"
        add("victims_count", "Число пострадавших", actual, str(truth.victims),
            kio.victims_count == truth.victims, "эталон сценария")

    required_fields = ([field for field in scenario.required_fields if field != "dds"]
                       if truth.incident_code else scenario.required_fields)
    if required_fields:
        missing = missing_fields(kio, required_fields)
        add("required_fields", "Обязательные поля карточки",
            "все заполнены" if not missing else "пусто: " + ", ".join(missing),
            "все обязательные поля заполнены", not missing,
            "поля КИО утверждённого сценария")

    return result
