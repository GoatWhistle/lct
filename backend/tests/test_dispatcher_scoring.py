from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.statuses import ServiceStatus, StatusEntry
from app.domain.taxonomy import ErrorCode
from app.scoring.dispatcher import dispatcher_metrics, evaluate_dispatcher


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
    state = SimpleNamespace(
        managed_services=lambda: ["Служба 101"],
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
        metric for metric in dispatcher_metrics(state, 30_000)
        if metric.key == "dds_reply"
    )

    assert not reply.passed
    assert "к каждой ручной отметке" in reply.norm

    findings = evaluate_dispatcher(
        entries=state.status_log,
        services=["Служба 101"],
        crew_assignments=state.crew_assignments,
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
    state = SimpleNamespace(
        managed_services=lambda: ["Служба 101"], status_log=entries,
        crew_assignments={}, dispatched_at=at,
    )
    metrics = dispatcher_metrics(
        state, 30_000, expected_decision="decline",
        expected_decision_reason="дублирующая карточка",
    )
    decision = next(metric for metric in metrics if metric.key == "dds_decision")
    assert decision.passed
    assert "дублирующая карточка" in decision.norm


def test_nonempty_unstructured_comment_fails_reply_metric_but_structured_passes():
    at = datetime.now(timezone.utc)
    state = SimpleNamespace(
        managed_services=lambda: ["Служба 101"],
        status_log=[StatusEntry(
            service="Служба 101", status=ServiceStatus.ACCEPTED,
            at=at, comment="бригада на связи",
        )],
        crew_assignments={"Служба 101": "Бригада 12"},
        dispatched_at=at,
    )
    reply = next(m for m in dispatcher_metrics(state, 30_000) if m.key == "dds_reply")
    assert not reply.passed
    findings = evaluate_dispatcher(
        entries=state.status_log, services=["Служба 101"],
        crew_assignments=state.crew_assignments,
        deadline_ms=30_000, elapsed_ms=0,
    )
    assert any("не разделяют основание и сведения" in f.summary for f in findings)

    state.status_log[0] = state.status_log[0].model_copy(update={
        "comment": "Основание: доклад старшего.\nСведения: бригада на связи.",
    })
    reply = next(m for m in dispatcher_metrics(state, 30_000) if m.key == "dds_reply")
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
    state = SimpleNamespace(
        managed_services=lambda: ["Служба 101"], status_log=entries,
        crew_assignments={"Служба 101": "Бригада 1"}, dispatched_at=at,
    )
    metrics = dispatcher_metrics(state, 30_000)
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
    state = SimpleNamespace(
        managed_services=lambda: ["Служба 101"], status_log=entries,
        crew_assignments={"Служба 101": "Бригада 1"}, dispatched_at=at,
    )
    metrics = dispatcher_metrics(state, 30_000)
    assert next(item for item in metrics if item.key == "dds_progress").passed
    assert next(item for item in metrics if item.key == "dds_completion").passed
    findings = evaluate_dispatcher(
        entries=entries, services=["Служба 101"],
        crew_assignments={"Служба 101": "Бригада 1"},
        deadline_ms=30_000, elapsed_ms=0,
    )
    assert ErrorCode.D6 not in [finding.code for finding in findings]
