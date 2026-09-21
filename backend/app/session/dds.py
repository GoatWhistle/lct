"""Выдача очередной учебной карточки ДДС в рамках одного занятия."""

import re

from app.domain.kio import KIO, ResponseStatus
from app.scenarios.schema import Scenario
from app.session.state import SessionState, now_utc
from app.session.timers import SessionTimers


def prepare_card(state: SessionState, scenario: Scenario) -> None:
    """Сменить активную карточку, сохранив общий ID занятия и прошлые результаты."""
    state.scenario = scenario.model_copy(deep=True)
    state.scenario_id = scenario.id
    state.scenario_title = scenario.title
    state.level = scenario.level.value
    state.required_fields = list(scenario.required_fields)
    truth = scenario.ground_truth
    address_fact = next((fact.value for fact in scenario.facts if "address" in fact.id), "")
    floor = re.search(r"(\d+)[-‑–]?й?\s*этаж", address_fact, re.IGNORECASE)
    service = truth.dds.value if truth.dds else None
    fallback = {"01": "Служба 101", "02": "МВД", "03": "Скорая помощь", "04": "Аварийная служба"}
    state.kio = KIO(
        registered_at=now_utc(), caller_number="+7 (495) 000-00-00",
        address=truth.address or address_fact or None,
        floor=floor.group(1) if floor else None,
        incident_type=truth.incident_type, incident_code=truth.incident_code,
        dds=truth.dds, signs=list(scenario.signs),
        notify=list(truth.notify) or ([fallback[service]] if service in fallback else []),
        victims_count=truth.victims,
        description="; ".join(fact.value for fact in scenario.facts[:3]) or scenario.first_line,
    )
    state.dispatched_card = None
    if service:
        state.dispatch(service)
    else:
        state.kio.response_status = ResponseStatus.TRANSFERRED
        state.dispatched_card = state.kio.model_copy(deep=True)
        state.dispatched_at = now_utc()

    state.status_log.clear()
    state.dds_log.clear()
    state.bounced_fields.clear()
    state.crew_selected = None
    state.crew_assignments.clear()
    state.phone_reports.clear()
    state.reply_text = ""
    state.reply_log.clear()
    state.timers = SessionTimers()
    state.on_event("dds.dispatch")
