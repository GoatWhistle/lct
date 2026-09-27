from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.statuses import PhoneReportRecord, ServiceStatus, StatusEntry
from app.domain.taxonomy import Competency, ErrorCode, FindingSource
from app.scenarios.loader import load_file
from app.scoring.dispatcher import dispatcher_metrics, evaluate_dispatcher
from app.scoring.timing import time_credit
from app.session.dds import DdsLiveCard, build_card
from app.session.timers import SessionTimers

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"
SERVICES = ["Служба 101"]


def live_card(*, status_log, crew_assignments, dispatched_at) -> DdsLiveCard:
    """Карточка пульта с заданным журналом статусов — вход оценки ДДС."""
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    card = build_card(scenario, 0, SessionTimers().limits)
    card.status_log = status_log
    card.crew_assignments = crew_assignments
    card.dispatched_at = dispatched_at
    return card


def test_late_primary_status_has_d1_finding_even_when_status_exists():
    findings = evaluate_dispatcher(
        entries=[StatusEntry(
            service="Служба 101",
            status=ServiceStatus.ACCEPTED,
            at=datetime.now(timezone.utc),
        )],
        services=["Служба 101"],
        deadline_ms=30_000,
        elapsed_ms=31_000,
    )

    d1 = next(finding for finding in findings if finding.code is ErrorCode.D1)
    assert "31 с" in d1.fact


def test_primary_status_within_deadline_does_not_have_d1_finding():
    findings = evaluate_dispatcher(
        entries=[StatusEntry(
            service="Служба 101",
            status=ServiceStatus.ACCEPTED,
            at=datetime.now(timezone.utc),
        )],
        services=["Служба 101"],
        deadline_ms=30_000,
        elapsed_ms=30_000,
    )

    assert ErrorCode.D1 not in [finding.code for finding in findings]


def test_d5_flags_comment_without_recipient_but_not_a_named_crew():
    at = datetime.now(timezone.utc)
    base = StatusEntry(
        service="Служба 101",
        status=ServiceStatus.ACCEPTED,
        at=at,
        comment="Основание: не обслуживаем.\nСведения: информация передана.",
    )
    findings = evaluate_dispatcher(
        entries=[base],
        services=["Служба 101"],
        deadline_ms=30_000,
        elapsed_ms=1_000,
    )
    d5 = next(finding for finding in findings if finding.code is ErrorCode.D5)
    assert "получателя" in d5.summary
    assert "передана" in d5.fact

    complete = evaluate_dispatcher(
        entries=[base.model_copy(update={"comment": "Основание: принято.\nСведения: карточка передана бригаде 12."})],
        services=["Служба 101"],
        deadline_ms=30_000,
        elapsed_ms=1_000,
    )
    assert ErrorCode.D5 not in [finding.code for finding in complete]


def test_recipient_in_one_status_comment_does_not_mask_another_status_comment():
    at = datetime.now(timezone.utc)
    entries = [
        StatusEntry(
            service="Служба 101", status=ServiceStatus.ACCEPTED,
            at=at, comment="Основание: карточка принята.\nСведения: переданы бригаде 12.",
        ),
        StatusEntry(
            service="Служба 101", status=ServiceStatus.RESPONDING,
            at=at, comment="Основание: доклад.\nСведения: подтверждено начало движения.",
        ),
    ]

    findings = evaluate_dispatcher(
        entries=entries,
        services=["Служба 101"],
        crew_assignments={"Служба 101": "Бригада 12"},
        deadline_ms=30_000,
        elapsed_ms=1_000,
    )

    d5 = next(finding for finding in findings if finding.code is ErrorCode.D5)
    assert "Начало реагирования" in d5.fact
    assert "Бригаде 12 переданы сведения" not in d5.fact


def test_missing_manual_status_comment_has_a_d5_finding():
    findings = evaluate_dispatcher(
        entries=[StatusEntry(
            service="Служба 101",
            status=ServiceStatus.ACCEPTED,
            at=datetime.now(timezone.utc),
        )],
        services=["Служба 101"],
        deadline_ms=30_000,
        elapsed_ms=1_000,
    )
    d5 = next(finding for finding in findings if finding.code is ErrorCode.D5)
    assert "не заполнены комментарии" in d5.fact


def test_each_manual_status_comment_is_part_of_the_numeric_reply_metric():
    at = datetime.now(timezone.utc)
    card = live_card(
        status_log=[
            StatusEntry(
                service="Служба 101", status=ServiceStatus.ACCEPTED,
                at=at, comment="Основание: карточка принята.\nСведения: принято в работу.",
            ),
            StatusEntry(
                service="Служба 101", status=ServiceStatus.RESPONDING,
                at=at, comment="",
            ),
        ],
        crew_assignments={"Служба 101": "Бригада 1"},
        dispatched_at=at,
    )

    reply = next(
        metric for metric in dispatcher_metrics(card, 30_000, services=SERVICES)
        if metric.key == "dds_reply"
    )

    assert not reply.passed
    assert "к каждой ручной отметке" in reply.norm

    findings = evaluate_dispatcher(
        entries=card.status_log,
        services=["Служба 101"],
        crew_assignments=card.crew_assignments,
        deadline_ms=30_000,
        elapsed_ms=0,
    )
    d5 = next(finding for finding in findings if finding.code is ErrorCode.D5)
    assert "Начало реагирования" in d5.fact


def test_d5_does_not_treat_address_house_number_as_recipient():
    entry = StatusEntry(
        service="Служба 101",
        status=ServiceStatus.ACCEPTED,
        at=datetime.now(timezone.utc),
        comment="Основание: доклад.\nСведения: в доме 101 проведён осмотр.",
    )
    findings = evaluate_dispatcher(
        entries=[entry],
        services=["Служба 101"],
        deadline_ms=30_000,
        elapsed_ms=1_000,
    )
    assert ErrorCode.D5 in [finding.code for finding in findings]


def test_scenario_may_define_a_valid_decline_for_duplicate_or_territory():
    at = datetime.now(timezone.utc)
    entries = [StatusEntry(
        service="Служба 101", status=ServiceStatus.DECLINED, at=at,
        comment="Основание: дубль.\nСведения: передано в дежурную часть.",
    )]
    findings = evaluate_dispatcher(
        entries=entries, services=["Служба 101"], deadline_ms=30_000,
        elapsed_ms=1_000, expected_decision="decline",
        expected_decision_reason="дублирующая карточка",
    )
    assert ErrorCode.D3 not in [finding.code for finding in findings]
    card = live_card(
        status_log=entries,
        crew_assignments={}, dispatched_at=at,
    )
    metrics = dispatcher_metrics(
        card, 30_000, expected_decision="decline",
        expected_decision_reason="дублирующая карточка", services=SERVICES,
    )
    decision = next(metric for metric in metrics if metric.key == "dds_decision")
    assert decision.passed
    assert "дублирующая карточка" in decision.norm


def test_nonempty_unstructured_comment_fails_reply_metric_but_structured_passes():
    at = datetime.now(timezone.utc)
    card = live_card(
        status_log=[StatusEntry(
            service="Служба 101", status=ServiceStatus.ACCEPTED,
            at=at, comment="бригада на связи",
        )],
        crew_assignments={"Служба 101": "Бригада 12"},
        dispatched_at=at,
    )
    reply = next(m for m in dispatcher_metrics(card, 30_000, services=SERVICES) if m.key == "dds_reply")
    assert not reply.passed
    findings = evaluate_dispatcher(
        entries=card.status_log, services=["Служба 101"],
        crew_assignments=card.crew_assignments,
        deadline_ms=30_000, elapsed_ms=0,
    )
    assert any("не разделяют основание и сведения" in f.summary for f in findings)

    card.status_log[0] = card.status_log[0].model_copy(update={
        "comment": "Основание: доклад старшего.\nСведения: бригада на связи.",
    })
    reply = next(m for m in dispatcher_metrics(card, 30_000, services=SERVICES) if m.key == "dds_reply")
    assert reply.passed

def test_accepting_card_with_scenario_expected_decline_is_explained():
    findings = evaluate_dispatcher(
        entries=[StatusEntry(
            service="Служба 101", status=ServiceStatus.ACCEPTED,
            at=datetime.now(timezone.utc),
        )],
        services=["Служба 101"], deadline_ms=30_000, elapsed_ms=1_000,
        expected_decision="decline", expected_decision_reason="не наша территория",
    )
    decision_finding = next(
        finding for finding in findings
        if finding.code is ErrorCode.D2 and "вопреки эталону" in finding.summary
    )
    assert decision_finding.norm == "не наша территория"


def test_scenario_decline_expectation_requires_an_explicit_reason():
    from app.scenarios.schema import DdsDecision

    with pytest.raises(ValidationError, match="reason обязателен"):
        DdsDecision(expected="decline")
    assert DdsDecision(expected="decline", reason="дубль").expected == "decline"


def test_incomplete_work_path_has_d6_for_failed_progress_metrics():
    at = datetime.now(timezone.utc)
    entries = [
        StatusEntry(service="Служба 101", status=ServiceStatus.ACCEPTED, at=at),
        StatusEntry(service="Служба 101", status=ServiceStatus.RESPONDING, at=at),
    ]
    card = live_card(
        status_log=entries,
        crew_assignments={"Служба 101": "Бригада 1"}, dispatched_at=at,
    )
    metrics = dispatcher_metrics(card, 30_000, services=SERVICES)
    assert not next(item for item in metrics if item.key == "dds_progress").passed
    assert not next(item for item in metrics if item.key == "dds_completion").passed
    findings = evaluate_dispatcher(
        entries=entries, services=["Служба 101"],
        crew_assignments={"Служба 101": "Бригада 1"},
        deadline_ms=30_000, elapsed_ms=0,
    )
    d6 = next(finding for finding in findings if finding.code is ErrorCode.D6)
    assert "Прибытие" in d6.fact and "Проведение работ" in d6.fact


def test_reasoned_refusal_after_acceptance_is_a_valid_terminal_path():
    at = datetime.now(timezone.utc)
    entries = [
        StatusEntry(service="Служба 101", status=ServiceStatus.ACCEPTED, at=at),
        StatusEntry(
            service="Служба 101", status=ServiceStatus.REFUSED, at=at,
            comment="Бригаде переданы сведения, выезд не выполнялся по причине угрозы.",
        ),
    ]
    card = live_card(
        status_log=entries,
        crew_assignments={"Служба 101": "Бригада 1"}, dispatched_at=at,
    )
    metrics = dispatcher_metrics(card, 30_000, services=SERVICES)
    assert next(item for item in metrics if item.key == "dds_progress").passed
    assert next(item for item in metrics if item.key == "dds_completion").passed
    findings = evaluate_dispatcher(
        entries=entries, services=["Служба 101"],
        crew_assignments={"Служба 101": "Бригада 1"},
        deadline_ms=30_000, elapsed_ms=0,
    )
    assert ErrorCode.D6 not in [finding.code for finding in findings]


# ── Реакция на доклад бригады (lct-37) ──

T0 = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
COMMENT = "Основание: доклад старшего.\nСведения: передано дежурному."


def at_second(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def mark(status: ServiceStatus, seconds: int) -> StatusEntry:
    return StatusEntry(service=SERVICES[0], status=status, at=at_second(seconds), comment=COMMENT)


def crew_report(phase: str, seconds: int) -> PhoneReportRecord:
    return PhoneReportRecord(service=SERVICES[0], crew="Бригада 12", phase=phase,
                             text="доклад", at=at_second(seconds))


def reaction(status_log, reports, limit_ms=45_000):
    card = live_card(status_log=status_log, crew_assignments={SERVICES[0]: "Бригада 12"},
                     dispatched_at=T0)
    card.phone_reports = reports
    findings = evaluate_dispatcher(
        entries=status_log, services=SERVICES, crew_assignments=card.crew_assignments,
        deadline_ms=30_000, elapsed_ms=5_000, phone_reports=reports,
        report_reaction_ms=limit_ms,
    )
    metric = next((m for m in dispatcher_metrics(card, 30_000, services=SERVICES,
                                                 report_reaction_ms=limit_ms)
                   if m.key == "dds_report_reaction"), None)
    return [f for f in findings if f.metric_key == "dds_report_reaction"], metric


def full_run(arrived_at: int) -> list[StatusEntry]:
    return [mark(ServiceStatus.ACCEPTED, 5), mark(ServiceStatus.RESPONDING, 20),
            mark(ServiceStatus.ARRIVED, arrived_at), mark(ServiceStatus.WORKING, arrived_at + 10),
            mark(ServiceStatus.COMPLETED, arrived_at + 20)]


def test_status_soon_after_crew_report_passes():
    findings, metric = reaction(full_run(arrived_at=110), [crew_report("arrived", 100)])

    assert findings == []
    assert metric.passed and metric.weight == 1.5
    assert metric.fact == (
        "статус «Прибытие» по докладу бригады «Бригада 12» о прибытии: "
        "10 с (на 35 с быстрее норматива)"
    )
    assert metric.credit == time_credit(10_000, 45_000)
    assert metric.title == f"{SERVICES[0]}: реакция на доклад бригады"


def test_status_later_than_limit_after_crew_report_is_d6_with_fact_and_norm():
    findings, metric = reaction(full_run(arrived_at=170), [crew_report("arrived", 100)])

    assert [f.code for f in findings] == [ErrorCode.D6]
    assert "«Прибытие»" in findings[0].fact and "70 с (+25 с сверх норматива)" in findings[0].fact
    assert "о прибытии" in findings[0].fact, "факт ссылается на доклад"
    assert findings[0].norm == metric.norm == "отметить статус по докладу за 45 с"
    assert findings[0].competency is Competency.NORMS, "отметка и метрика в одной компетенции"
    assert findings[0].source is FindingSource.DISPATCHER
    assert findings[0].service == SERVICES[0]
    assert not metric.passed and metric.fact == findings[0].fact
    assert metric.credit == time_credit(70_000, 45_000)


def test_violation_by_a_fraction_of_a_second_is_not_shown_as_the_limit():
    status_log = full_run(arrived_at=170)
    status_log[2] = status_log[2].model_copy(
        update={"at": at_second(100) + timedelta(milliseconds=45_400)},
    )

    findings, metric = reaction(status_log, [crew_report("arrived", 100)])

    assert not metric.passed
    assert "45.4 с (+0.4 с сверх норматива)" in findings[0].fact


def test_missing_status_after_crew_report_is_d6():
    status_log = [mark(ServiceStatus.ACCEPTED, 5), mark(ServiceStatus.RESPONDING, 20)]

    findings, metric = reaction(status_log, [crew_report("arrived", 100)])

    assert [f.code for f in findings] == [ErrorCode.D6]
    assert "не отмечен" in findings[0].fact and "Бригада 12" in findings[0].fact
    assert not metric.passed and metric.credit == 0.0
    assert metric.fact == "статус «Прибытие» по докладу бригады «Бригада 12» о прибытии: не отмечен"


def test_metric_takes_the_worst_report_and_counts_violations():
    reports = [crew_report("dispatched", 10), crew_report("arrived", 100),
               crew_report("working", 175)]

    findings, metric = reaction(full_run(arrived_at=170), reports)

    assert len(findings) == 1, "одна отметка на службу, как у других нормативов"
    assert "о прибытии: 70 с" in metric.fact
    assert "с нарушением 1 из 3 докладов" in metric.fact


def test_status_set_before_the_report_and_custom_limit():
    findings, metric = reaction(full_run(arrived_at=90), [crew_report("arrived", 100)])
    assert findings == [] and metric.passed, "статус раньше доклада — не нарушение"
    assert metric.fact.endswith("отмечен не позже доклада") and metric.credit == 1.0

    strict, _ = reaction(full_run(arrived_at=130), [crew_report("arrived", 100)], limit_ms=20_000)
    assert strict[0].norm == "отметить статус по докладу за 20 с"


def test_no_crew_reports_means_no_reaction_metric():
    findings, metric = reaction(full_run(arrived_at=110), [])

    assert findings == [] and metric is None


def test_refusal_after_crew_report_is_neither_d6_nor_failed_metric():
    status_log = [
        mark(ServiceStatus.ACCEPTED, 5), mark(ServiceStatus.RESPONDING, 20),
        StatusEntry(service=SERVICES[0], status=ServiceStatus.REFUSED, at=at_second(120),
                    comment="Основание: доклад старшего.\nСведения: бригаде отказано в доступе."),
    ]

    findings, metric = reaction(status_log, [crew_report("arrived", 100)])

    assert findings == [], "работ не было — статус «Прибытие» отмечать не нужно"
    assert metric is None, "метрика не снижает балл без отметки"


def test_refusal_keeps_late_reaction_on_reported_status():
    status_log = [
        mark(ServiceStatus.ACCEPTED, 5), mark(ServiceStatus.RESPONDING, 80),
        StatusEntry(service=SERVICES[0], status=ServiceStatus.REFUSED, at=at_second(120),
                    comment="Основание: доклад старшего.\nСведения: бригаде отказано в доступе."),
    ]

    findings, metric = reaction(status_log, [crew_report("dispatched", 10),
                                             crew_report("arrived", 100)])

    assert [f.code for f in findings] == [ErrorCode.D6]
    assert not metric.passed and "о выезде: 70 с" in metric.fact
    assert "докладов" not in metric.fact, "неотмеченное прибытие при отказе не считается"


def test_unfinished_work_after_completion_report_names_the_report():
    status_log = full_run(arrived_at=110)[:-1]
    findings = evaluate_dispatcher(
        entries=status_log, services=SERVICES, crew_assignments={SERVICES[0]: "Бригада 12"},
        deadline_ms=30_000, elapsed_ms=5_000, phone_reports=[crew_report("completed", 150)],
    )

    unfinished = next(f for f in findings if f.summary.endswith("не доведён до конца"))
    assert "бригада доложила о завершении работ" in unfinished.fact
