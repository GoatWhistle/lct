"""Одновременная выдача учебных карточек ДДС в рамках одного занятия."""

import re
from datetime import datetime, timedelta
from uuid import uuid4

from app.domain import ekp
from app.domain.kio import KIO, ResponseStatus
from app.scenarios.schema import Scenario
from app.session.state import DdsLiveCard, SessionState, now_utc
from app.session.timers import SessionTimers


def prepare_card(state: SessionState, scenario: Scenario) -> None:
    """Сменить активную карточку, сохранив общий ID занятия и прошлые результаты."""
    state.scenario = scenario.model_copy(deep=True)
    state.scenario_id = scenario.id
    state.scenario_title = scenario.title
    state.level = scenario.level.value
    state.required_fields = list(scenario.required_fields)
    truth = scenario.ground_truth
    address_fact = next(
        (fact.value for fact in scenario.facts if "address" in fact.id), ""
    )
    caller_fact = next(
        (
            fact.value
            for fact in scenario.facts
            if fact.id in {"f_caller", "f_applicant"}
        ),
        "",
    )
    caller_phone = next(
        (
            match.group(0).strip()
            for match in re.finditer(
                r"(?<!\d)(?:\+?7|8)?(?:[\s().-]*\d){10,11}(?!\d)", caller_fact
            )
            if len(re.sub(r"\D", "", match.group(0))) in {10, 11}
        ),
        None,
    )
    caller_names = re.findall(
        r"(?<![А-ЯЁа-яё])([А-ЯЁ][а-яё-]+(?:\s+[А-ЯЁ][а-яё-]+){1,2})(?![А-ЯЁа-яё])",
        caller_fact,
    )
    # Some ticket facts name multiple witnesses; don't silently attribute the
    # contact number to the first person when the source doesn't disambiguate.
    caller_name = caller_names[0] if len(caller_names) == 1 else None
    floor = re.search(r"(\d+)[-‑–]?й?\s*этаж", address_fact, re.IGNORECASE)
    service = truth.dds.value if truth.dds else None
    fallback = {
        "01": "Служба 101",
        "02": "МВД",
        "03": "Скорая помощь",
        "04": "Аварийная служба",
    }
    if scenario.student_card is not None:
        # A moderated learner-authored KIO is the card itself; do not flatten
        # it to title/description and silently discard its structured fields.
        state.kio = scenario.student_card.model_copy(
            deep=True,
            update={
                "card_id": uuid4(),
                "registered_at": now_utc(),
                "response_status": ResponseStatus.REGISTERED,
                "dispatch_order_at": None,
                "arrival_at": None,
            },
        )
    else:
        state.kio = KIO(
            registered_at=now_utc(),
            caller_number=caller_phone,
            caller_name=caller_name,
            caller_contact=caller_phone,
            address=truth.address or address_fact or None,
            floor=floor.group(1) if floor else None,
            incident_type=truth.incident_type,
            incident_code=truth.incident_code,
            incident_group=(
                ekp.incident(truth.incident_code).group
                if truth.incident_code and ekp.incident(truth.incident_code)
                else None
            ),
            dds=truth.dds,
            signs=list(scenario.signs),
            notify=list(truth.notify)
            or ([fallback[service]] if service in fallback else []),
            victims_count=truth.victims,
            description="; ".join(fact.value for fact in scenario.facts[:3])
            or scenario.first_line,
        )
    state.dispatched_card = None
    if service:
        state.dispatch(service)
    else:
        state.kio.response_status = ResponseStatus.TRANSFERRED
        state.dispatched_card = state.kio.model_copy(deep=True)
        state.dispatched_at = now_utc()

    # A live DДС card keeps references to the current working collections.
    # Never clear them in place when preparing the next card: that would also
    # erase the previous card's history. Replace the working collections so
    # every queued card retains an isolated snapshot.
    state.status_log = []
    state.dds_log = []
    state.bounced_fields = []
    state.crew_selected = None
    state.crew_assignments = {}
    state.phone_reports = []
    state.phone_lines = []
    state.phone_pending = None
    state.reply_text = ""
    state.reply_log = []
    # Настроенный преподавателем лимит копируется в независимый таймер карточки.
    state.timers = SessionTimers(limits=dict(state.timers.limits))
    state.on_event("dds.dispatch")


def _append_live_card(
    state: SessionState, scenario: Scenario, index: int
) -> DdsLiveCard:
    state.dds_card_index = index
    prepare_card(state, scenario)
    card = DdsLiveCard(
        original_index=index,
        scenario=state.scenario,
        kio=state.kio,
        dispatched_card=state.dispatched_card,
        dispatched_at=state.dispatched_at,
        timers=state.timers,
        bounced_fields=state.bounced_fields,
        dds_log=state.dds_log,
        status_log=state.status_log,
        crew_selected=state.crew_selected,
        crew_assignments=state.crew_assignments,
        phone_reports=state.phone_reports,
        phone_lines=state.phone_lines,
        phone_pending=state.phone_pending,
        reply_text=state.reply_text,
        reply_log=state.reply_log,
    )
    state.dds_live_cards.append(card)
    return card


def deliver_due_cards(state: SessionState, now: datetime | None = None) -> int:
    """Выдать всё, чему настал срок и для чего есть место в ожидании.

    Активная карточка не считается ожидающей. Каждый вызов prepare_card
    запускает собственный 30-секундный таймер ровно при поступлении.
    """
    now = now or now_utc()
    if state.dds_next_arrival_at is None or state.dds_next_arrival_at > now:
        return 0

    delivered = 0
    while state.dds_next_scenario_index < len(state.dds_scenarios):
        active_id = (
            state.dds_active_card_id
            if any(
                item.card_id == state.dds_active_card_id
                for item in state.dds_live_cards
            )
            else None
        )
        active_exists = active_id is not None
        waiting_count = len(state.dds_live_cards) - int(active_exists)
        if waiting_count >= state.dds_max_waiting:
            break

        index = state.dds_next_scenario_index
        card = _append_live_card(state, state.dds_scenarios[index], index)
        state.dds_next_scenario_index += 1
        delivered += 1
        if not active_exists:
            state.activate_dds_card(card.card_id, capture=False)
        else:
            # prepare_card builds the new snapshot through SessionState's
            # working fields; restore the trainee's active card immediately.
            state.activate_dds_card(active_id, capture=False)

        if state.dds_next_scenario_index >= len(state.dds_scenarios):
            state.dds_next_arrival_at = None
            break
        if state.dds_arrival_interval_seconds:
            # Pace from actual delivery time. If the cap was full, the next
            # card becomes due one full interval after this release.
            state.dds_next_arrival_at = card.dispatched_at + timedelta(
                seconds=state.dds_arrival_interval_seconds
            )
            break
        # Zero interval means fill active + available waiting slots now.
        state.dds_next_arrival_at = now

    return delivered


def prepare_queue(
    state: SessionState,
    scenarios: list[Scenario],
    arrival_interval_seconds: int = 0,
    max_waiting: int = 3,
) -> None:
    """Начать поступление очереди с первой карточки; остальные идут по темпу."""
    state.dds_live_cards.clear()
    state.dds_scenarios = scenarios
    state.dds_arrival_interval_seconds = arrival_interval_seconds
    state.dds_max_waiting = max_waiting
    state.dds_next_scenario_index = 0
    state.dds_next_arrival_at = now_utc()
    deliver_due_cards(state)


def prepare_handoff_queue(
    state: SessionState,
    additional_scenarios: list[Scenario],
    arrival_interval_seconds: int = 0,
    max_waiting: int = 3,
) -> None:
    """Передать созданную курсантом карточку в ДДС перед готовыми карточками."""
    if state.dispatched_card is None or state.scenario is None:
        return
    now = now_utc()
    state.operator_kio = state.dispatched_card.model_copy(deep=True)
    state.operator_scenario = state.scenario.model_copy(deep=True)
    # Карточка курсанта становится первым живым объектом очереди. Её данные
    # не переписываются из эталона: именно их будет обрабатывать ДДС.
    dds_timers = SessionTimers(limits=dict(state.timers.limits))
    dds_timers.on_event("dds.dispatch")
    first = DdsLiveCard(
        original_index=0,
        scenario=state.operator_scenario,
        kio=state.operator_kio.model_copy(deep=True),
        dispatched_card=state.operator_kio.model_copy(deep=True),
        dispatched_at=state.dispatched_at or now,
        timers=dds_timers,
    )
    state.dds_live_cards = [first]
    state.dds_scenarios = [state.operator_scenario, *additional_scenarios]
    state.dds_arrival_interval_seconds = arrival_interval_seconds
    state.dds_max_waiting = max_waiting
    state.dds_next_scenario_index = 1
    state.dds_next_arrival_at = (
        (
            now + timedelta(seconds=arrival_interval_seconds)
            if additional_scenarios and arrival_interval_seconds
            else now
        )
        if additional_scenarios
        else None
    )
    state.activate_dds_card(first.card_id, capture=False)
    deliver_due_cards(state)
