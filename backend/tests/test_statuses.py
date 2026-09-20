"""Статусы реагирования на АРМ ДДС.

Пункты приёмки lct-33: автомат переходов, обязательный комментарий,
необратимость завершения, статус карточки и ошибки диспетчера D1–D6.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.statuses import (
    CardStatus,
    ServiceStatus,
    StatusEntry,
    StatusError,
    card_status,
    check,
    current,
)
from app.domain.taxonomy import ErrorCode
from app.scoring.dispatcher import evaluate_dispatcher

SERVICES = ["Служба 101", "МВД"]
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def mark(service: str, status: ServiceStatus, comment: str = "", minute: int = 0) -> StatusEntry:
    return StatusEntry(
        service=service, status=status, at=NOW + timedelta(minutes=minute), comment=comment
    )


# ── автомат переходов ──


def test_only_primary_statuses_are_available_at_first():
    check([], "Служба 101", ServiceStatus.ACCEPTED, "")
    check([], "Служба 101", ServiceStatus.DECLINED, "не обслуживаем, передано в УК")
    with pytest.raises(StatusError):
        check([], "Служба 101", ServiceStatus.ARRIVED, "")


def test_accepted_opens_the_rest():
    log = [mark("Служба 101", ServiceStatus.ACCEPTED)]
    check(log, "Служба 101", ServiceStatus.RESPONDING, "")
    check(log, "Служба 101", ServiceStatus.COMPLETED, "")


def test_declined_leads_only_back_to_accepted():
    """Служба может передумать, но не может отказаться дважды по-разному."""
    log = [mark("Служба 101", ServiceStatus.DECLINED, "не наш адрес")]
    check(log, "Служба 101", ServiceStatus.ACCEPTED, "")
    with pytest.raises(StatusError):
        check(log, "Служба 101", ServiceStatus.RESPONDING, "")


def test_completed_closes_the_card_for_editing():
    log = [
        mark("Служба 101", ServiceStatus.ACCEPTED),
        mark("Служба 101", ServiceStatus.COMPLETED, minute=5),
    ]
    with pytest.raises(StatusError) as exc:
        check(log, "Служба 101", ServiceStatus.WORKING, "")
    assert "закрыта" in str(exc.value)


def test_refusal_without_a_comment_is_rejected():
    """Не валидация формы, а предмет обучения: половина нарушений в памятке —
    отказ без указания, куда передана информация."""
    with pytest.raises(StatusError) as exc:
        check([], "МВД", ServiceStatus.DECLINED, "   ")
    assert "комментар" in str(exc.value)


def test_statuses_are_tracked_per_service():
    log = [mark("Служба 101", ServiceStatus.ACCEPTED)]
    assert current(log, "Служба 101") is ServiceStatus.ACCEPTED
    assert current(log, "МВД") is ServiceStatus.ADDED


# ── статус карточки ──


def test_silence_past_the_deadline_is_not_notified():
    assert card_status([], SERVICES, notify_deadline_passed=True) is CardStatus.NOT_NOTIFIED


def test_silence_within_the_deadline_is_still_registered():
    assert card_status([], SERVICES, notify_deadline_passed=False) is CardStatus.REGISTERED


def test_any_refusal_marks_the_whole_card():
    log = [
        mark("Служба 101", ServiceStatus.ACCEPTED),
        mark("МВД", ServiceStatus.DECLINED, "не наша территория, передано в ОМВД"),
    ]
    assert card_status(log, SERVICES) is CardStatus.REFUSED


def test_all_completed_closes_the_card():
    log = []
    for service in SERVICES:
        log += [mark(service, ServiceStatus.ACCEPTED), mark(service, ServiceStatus.COMPLETED, minute=9)]
    assert card_status(log, SERVICES) is CardStatus.COMPLETED


def test_alarming_statuses_are_the_three_from_the_memo():
    from app.domain.statuses import ALARMING

    assert ALARMING == {CardStatus.NOT_NOTIFIED, CardStatus.REFUSED, CardStatus.NOT_COMPLETED}


# ── ошибки диспетчера ──


def _codes(entries, services=SERVICES, elapsed=45_000):
    findings = evaluate_dispatcher(
        entries=entries, services=services, deadline_ms=30_000, elapsed_ms=elapsed
    )
    return [finding.code for finding in findings]


def test_no_status_is_d1():
    assert ErrorCode.D1 in _codes([])


def test_refusal_without_comment_is_d4():
    """Автомат такую отметку не пропустит, но карточка может прийти из внешней
    системы уже с пустым комментарием — разбор обязан это показать."""
    log = [mark(service, ServiceStatus.DECLINED) for service in SERVICES]
    assert ErrorCode.D4 in _codes(log)


def test_declining_a_profile_incident_is_d3():
    log = [
        mark("Служба 101", ServiceStatus.ACCEPTED),
        mark("МВД", ServiceStatus.DECLINED, "не в нашей компетенции"),
    ]
    codes = _codes(log)
    assert ErrorCode.D3 in codes, "служба в списке оповещения — происшествие профильное"


def test_accepted_and_forgotten_is_d2():
    log = [mark(service, ServiceStatus.ACCEPTED) for service in SERVICES]
    assert ErrorCode.D2 in _codes(log)


def test_completed_without_progress_is_d6():
    log = []
    for service in SERVICES:
        log += [mark(service, ServiceStatus.ACCEPTED), mark(service, ServiceStatus.COMPLETED, minute=9)]
    assert ErrorCode.D6 in _codes(log)


def test_clean_work_has_no_findings():
    log = []
    for service in SERVICES:
        log += [
            mark(service, ServiceStatus.ACCEPTED),
            mark(service, ServiceStatus.RESPONDING, minute=1),
            mark(service, ServiceStatus.ARRIVED, minute=4),
            mark(service, ServiceStatus.WORKING, minute=5),
            mark(service, ServiceStatus.COMPLETED, minute=20),
        ]
    assert _codes(log) == []


def test_every_finding_carries_its_reason():
    """Правило проекта: ни одной отметки без обоснования."""
    findings = evaluate_dispatcher(
        entries=[], services=SERVICES, deadline_ms=30_000, elapsed_ms=45_000
    )
    assert findings
    for finding in findings:
        assert finding.fact and finding.ref
