"""Пульт ДДС без сокетов и хаба: очередь, переключение, завершение, темп."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from app.domain.statuses import ServiceStatus
from app.domain.timers import TimerCode
from app.scenarios.loader import load_file
from app.session.dds import DdsCardRecord, DdsDesk
from app.session.timers import SessionTimers, now_utc

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"
NAMES = ("fire-apartment-l2.yaml", "tickets/t20-2-stroke.yaml",
         "tickets/t01-1-fire-container.yaml")


def scenarios(count: int = 3):
    return [load_file(LIBRARY / name, LIBRARY) for name in NAMES[:count]]


def record(card) -> DdsCardRecord:
    return DdsCardRecord(card_id=card.card_id, scenario_id=card.scenario.id, reply_text="",
                         metrics=[], findings=[], actions=[], duration_ms=0)


def started(interval: int = 0, max_waiting: int = 3, now=None) -> DdsDesk:
    desk = DdsDesk()
    desk.start(scenarios(), limits=SessionTimers().limits,
               arrival_interval_seconds=interval, max_waiting=max_waiting, now=now or now_utc())
    return desk


def test_first_card_becomes_active_and_rest_wait_when_no_pace():
    desk = started()

    assert [card.scenario.id for card in desk.ordered()] == [
        "fire-apartment-l2", "t20-2-stroke", "t01-1-fire-container"]
    assert desk.active is desk.ordered()[0]
    assert desk.next_arrival_at is None


def test_paced_queue_delivers_next_card_only_after_interval():
    start = now_utc()
    desk = started(interval=60, now=start)
    first = desk.active

    assert desk.deliver_due(start + timedelta(seconds=59)) == 0
    assert desk.deliver_due(start + timedelta(seconds=61)) == 1

    assert len(desk.cards) == 2
    assert desk.active is first


def test_waiting_cap_holds_back_delivery():
    desk = started(max_waiting=1)

    assert len(desk.cards) == 2
    assert desk.next_index == 2


def test_open_switches_by_reference_without_copying_fields():
    desk = started()
    first, second = desk.ordered()[:2]
    first.status_log.append(object())

    assert desk.open(second.card_id)
    second.reply_text = "ответ по второй"
    assert desk.open(first.card_id)

    assert desk.active is first
    assert len(first.status_log) == 1 and second.status_log == []
    assert second.reply_text == "ответ по второй" and first.reply_text == ""
    assert desk.card_index == 0


def test_open_unknown_card_keeps_active():
    desk = started()
    active = desk.active

    assert not desk.open(uuid4())
    assert desk.active is active


def test_complete_moves_to_following_card_and_keeps_record():
    desk = started()
    first, second, _third = desk.ordered()

    following = desk.complete(first.card_id, record(first))

    assert following is second and desk.active is second
    assert first.card_id not in desk.cards
    assert desk.is_completed(first.card_id)


def test_completing_last_card_leaves_desk_between_cards_with_last_index():
    desk = started(interval=60, max_waiting=1)
    only = desk.active

    assert desk.complete(only.card_id, record(only)) is None

    assert desk.active is None and desk.active_id is None
    assert desk.card_index == 0
    assert desk.deliver_due(now_utc() + timedelta(seconds=61)) == 1
    assert desk.active.scenario.id == "t20-2-stroke"


def test_each_card_runs_own_decision_timer_with_configured_limit():
    desk = DdsDesk()
    limits = {**SessionTimers().limits, TimerCode.DDS_ACK: 45_000}
    desk.start(scenarios(2), limits=limits, now=now_utc())
    first, second = desk.ordered()

    assert first.timers is not second.timers
    assert first.timers.limits[TimerCode.DDS_ACK] == 45_000
    assert TimerCode.DDS_ACK in second.timers.timers


def test_card_status_mark_lands_in_that_card_only():
    desk = started()
    first, second = desk.ordered()[:2]
    service = first.dispatched_card.notify[0]

    first.set_service_status(service, ServiceStatus.ACCEPTED, "Основание: доклад")

    assert [mark.status for mark in first.status_log] == [ServiceStatus.ACCEPTED]
    assert second.status_log == []
