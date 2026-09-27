"""Правила пульта ДДС без сокета, хаба и БД: `DdsDesk.apply`."""

import copy
from pathlib import Path
from uuid import uuid4

from app.domain.events import (
    CardAck,
    CardBounce,
    CardNext,
    CrewSelect,
    Exercise,
    PhoneBrief,
    PhoneCheck,
    PhoneDial,
    PhoneLine,
    PhoneReport,
    ServiceStatusSet,
    SessionMode,
    StationFinish,
)
from app.domain.statuses import ServiceStatus
from app.domain.timers import TimerCode
from app.scenarios.loader import load_file
from app.session.dds import prepare_queue
from app.session.state import SessionState

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"
NAMES = ("fire-apartment-l2.yaml", "tickets/t20-2-stroke.yaml")
BASIS = "Основание: доклад старшего. Сведения: карточка принята."


def lesson(count: int = 2) -> SessionState:
    scenarios = [load_file(LIBRARY / name, LIBRARY) for name in NAMES[:count]]
    state = SessionState(
        session_id=uuid4(), scenario_id=scenarios[0].id, scenario_title=scenarios[0].title,
        level=scenarios[0].level.value, mode=SessionMode.TRAINING, exercise=Exercise.DDS,
    )
    prepare_queue(state, scenarios)
    return state


def service_of(state: SessionState) -> str:
    return state.card_services(state.desk.active)[0]


def test_ack_without_comment_is_refused():
    state = lesson()

    outcome = state.desk.apply(CardAck.model_construct(comment="  "), state)

    assert outcome.error == "Для подтверждения приёма добавьте комментарий с основанием"
    assert outcome.events == [] and not outcome.changed


def test_repeated_ack_changes_nothing():
    state = lesson()
    card = state.desk.active
    assert state.desk.apply(CardAck(comment=BASIS), state).changed
    log, statuses = list(card.dds_log), list(card.status_log)

    outcome = state.desk.apply(CardAck(comment=BASIS), state)

    assert outcome.error is None and not outcome.changed and outcome.events == []
    assert card.dds_log == log and card.status_log == statuses


def test_status_of_foreign_service_is_refused():
    state = lesson()

    outcome = state.desk.apply(
        ServiceStatusSet(service="Чужая служба", status=ServiceStatus.ACCEPTED, comment=BASIS),
        state,
    )

    assert outcome.error == "Можно менять статусы только своей ДДС"


def test_primary_status_stops_decision_timer():
    state = lesson()
    card = state.desk.active
    assert card.timers.measured_ms(TimerCode.DDS_ACK) is None

    outcome = state.desk.apply(
        ServiceStatusSet(service=service_of(state), status=ServiceStatus.ACCEPTED,
                         comment=BASIS),
        state,
    )

    assert outcome.error is None and outcome.changed
    assert card.timers.measured_ms(TimerCode.DDS_ACK) is not None
    assert card.timers.timers[TimerCode.DDS_WORK].started_at is not None


def test_dial_before_required_status_is_refused():
    state = lesson()
    crew = state.crew_options(state.desk.active)[0]
    assert state.desk.apply(CrewSelect(crew=crew), state).changed

    outcome = state.desk.apply(PhoneDial(), state)

    assert outcome.error == "Перед звонком нужен статус «Принята» этой службы"
    assert state.desk.active.phone_pending is None


def test_crew_reports_follow_four_phases_in_order():
    state = lesson()
    desk, card, service = state.desk, state.desk.active, service_of(state)
    crew = state.crew_options(card)[0]

    def mark(status):
        outcome = desk.apply(ServiceStatusSet(service=service, status=status, comment=BASIS), state)
        assert outcome.error is None

    def call(command):
        dial = desk.apply(PhoneDial(), state)
        assert dial.error is None and [type(event) for event in dial.events] == [PhoneLine]
        talk = desk.apply(command, state)
        assert talk.error is None
        assert [type(event) for event in talk.events] == [PhoneLine, PhoneLine, PhoneReport]
        return talk.events[-1]

    mark(ServiceStatus.ACCEPTED)
    desk.apply(CrewSelect(crew=crew), state)
    reports = [call(PhoneBrief(address=card.dispatched_card.address,
                               incident="Пожар в квартире, есть задымление",
                               request="Прошу выезд бригады"))]
    for status in (ServiceStatus.RESPONDING, ServiceStatus.ARRIVED, ServiceStatus.WORKING):
        mark(status)
        reports.append(call(PhoneCheck(text="Доложите обстановку на месте")))

    assert [report.phase for report in reports] == [
        "dispatched", "arrived", "working", "completed"]
    assert [report.phase for report in card.phone_reports] == [
        "dispatched", "arrived", "working", "completed"]
    assert desk.apply(PhoneDial(), state).error == "Все доклады этой бригады уже получены"


def test_next_with_foreign_card_id_is_refused():
    state = lesson()

    outcome = state.desk.apply(CardNext(card_id=uuid4()), state)

    assert outcome.error == "Следующая карточка недоступна: ID текущей не совпадает"


def test_next_on_last_card_signals_end_of_lesson():
    state = lesson(count=2)
    first = state.desk.active

    middle = state.desk.apply(CardNext(card_id=first.card_id), state)
    assert middle.error is None and not middle.finished

    last = state.desk.apply(CardNext(card_id=state.desk.active.card_id), state)

    assert last.error is None and last.finished and last.changed
    assert state.desk.active is None and len(state.desk.completed) == 2
    assert not state.ended, "занятие завершает сессия, пульт только сигналит"


def test_station_finish_stops_work_timers_and_signals_end():
    state = lesson()
    card = state.desk.active
    state.desk.apply(CardAck(comment=BASIS), state)

    outcome = state.desk.apply(StationFinish(), state)

    assert outcome.finished and outcome.error is None
    assert card.timers.timers[TimerCode.DDS_WORK].stopped


def test_bounce_is_refused_in_favour_of_112_control():
    state = lesson()

    outcome = state.desk.apply(CardBounce(missing_fields=["floor"]), state)

    assert outcome.error == (
        "Карточку не возвращают в 112 при принятии и не правят на АРМ ДДС. "
        "Если ошибка вскрылась по докладу бригады с места — сообщите о ней в 112 по телефону")


def test_refusals_leave_desk_untouched():
    state = lesson()
    desk, card, service = state.desk, state.desk.active, service_of(state)
    refused = [
        ServiceStatusSet(service=service, status=ServiceStatus.COMPLETED, comment=""),
        ServiceStatusSet(service="Чужая служба", status=ServiceStatus.ARRIVED, comment=BASIS),
        CrewSelect(crew="Бригада не из списка"),
        PhoneDial(),
        PhoneCheck(text="Доложите обстановку на месте"),
        CardNext(card_id=uuid4()),
        CardBounce(missing_fields=["floor"]),
    ]
    before = copy.deepcopy(desk)

    for command in refused:
        outcome = desk.apply(command, state)
        assert outcome.error is not None, command
        assert outcome.events == [] and not outcome.changed and not outcome.finished

    assert desk == before
    assert desk.active is card
