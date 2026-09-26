"""Переносимый JSON-снимок незавершённого занятия.

Снимок хранится в PostgreSQL после каждого подтверждённого действия. Он не
содержит сокеты, аудиобуферы или объекты моделей: после перезапуска процесса
они создаются заново, а учебные данные, таймеры и состояние ДДС восстанавливаются.
"""

import time
from datetime import UTC, datetime
from uuid import UUID

from fastapi.encoders import jsonable_encoder

from app.dialog.factory import build_caller
from app.dialog.persona import PersonaState
from app.dialog.runtime import get_embedder
from app.dialog.slots import SlotMachine
from app.domain.events import (
    CallEndReason,
    Exercise,
    LessonCriteria,
    Metric,
    SessionMode,
    TranscriptEntry,
)
from app.domain.kio import KIO
from app.domain.statuses import (
    PhoneCallPending,
    PhoneLineRecord,
    PhoneReportRecord,
    StatusEntry,
)
from app.domain.taxonomy import Finding
from app.domain.timers import TimerCode
from app.scenarios.schema import Scenario
from app.scoring.grammar import GrammarAssessment
from app.session.state import DdsCardRecord, DdsLiveCard, SessionState, now_utc
from app.session.timers import SessionTimers, Timer

CHECKPOINT_VERSION = 1


def _grammar(value: GrammarAssessment | None) -> dict | None:
    if value is None:
        return None
    return {"passed": value.passed, "errors": list(value.errors), "source": value.source}


def _dump_timers(timers: SessionTimers, now: float) -> dict:
    return {
        "limits": {code.value: limit for code, limit in timers.limits.items()},
        "items": {
            code.value: {
                "elapsed_ms": timer.current_ms(now),
                "attempt": timer.attempt,
                "stopped": timer.stopped,
            }
            for code, timer in timers.timers.items()
        },
    }


def _dump_live_card(item: DdsLiveCard, now: float) -> dict:
    return {
        "original_index": item.original_index,
        "scenario": item.scenario.model_dump(mode="json"),
        "kio": item.kio.model_dump(mode="json"),
        "dispatched_card": item.dispatched_card.model_dump(mode="json"),
        "dispatched_at": item.dispatched_at.isoformat(),
        "timers": _dump_timers(item.timers, now),
        "bounced_fields": item.bounced_fields,
        "dds_log": [[action, at.isoformat(), detail] for action, at, detail in item.dds_log],
        "status_log": [entry.model_dump(mode="json") for entry in item.status_log],
        "crew_selected": item.crew_selected,
        "crew_assignments": item.crew_assignments,
        "phone_reports": [entry.model_dump(mode="json") for entry in item.phone_reports],
        "phone_lines": [entry.model_dump(mode="json") for entry in item.phone_lines],
        "phone_pending": item.phone_pending.model_dump(mode="json") if item.phone_pending else None,
        "reply_text": item.reply_text,
        "reply_grammar": _grammar(item.reply_grammar),
        "reply_log": [[at.isoformat(), text] for at, text in item.reply_log],
    }


def dump_state(state: SessionState) -> dict:
    """Сериализовать только данные, необходимые для точного продолжения."""
    state.capture_active_dds()
    now = time.monotonic()
    payload = {
        "version": CHECKPOINT_VERSION,
        "session_id": str(state.session_id),
        "scenario_id": state.scenario_id,
        "scenario_title": state.scenario_title,
        "level": state.level,
        "mode": state.mode.value,
        "owner_login": state.owner_login,
        "backend_fencing_epoch": state.backend_fencing_epoch,
        "exercise": state.exercise.value,
        "handoff_to_dds": state.handoff_to_dds,
        "required_fields": state.required_fields,
        "trainee_name": state.trainee_name,
        "trainee_id": str(state.trainee_id) if state.trainee_id else None,
        "dds_service": state.dds_service,
        "attempt": state.attempt,
        "criteria": state.criteria.model_dump(mode="json"),
        "kio": state.kio.model_dump(mode="json"),
        "transcript": [entry.model_dump(mode="json") for entry in state.transcript],
        "timers": _dump_timers(state.timers, now),
        "hints_shown": state.hints_shown,
        "hints_log": [[item, at.isoformat()] for item, at in state.hints_log],
        "notes": state.notes,
        "directives": state.directives,
        "scenario": state.scenario.model_dump(mode="json") if state.scenario else None,
        "audio_frames": state.audio_frames,
        "bad_frames": state.bad_frames,
        "self_assessed": state.self_assessed,
        "self_assessment": state.self_assessment,
        "score": state.score,
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "ended_at": state.ended_at.isoformat() if state.ended_at else None,
        "end_reason": state.end_reason.value if state.end_reason else None,
        "dispatched_card": (
            state.dispatched_card.model_dump(mode="json") if state.dispatched_card else None
        ),
        "dispatched_at": state.dispatched_at.isoformat() if state.dispatched_at else None,
        "bounced_fields": state.bounced_fields,
        "dds_log": [
            [action, at.isoformat(), detail] for action, at, detail in state.dds_log
        ],
        "status_log": [item.model_dump(mode="json") for item in state.status_log],
        "crew_selected": state.crew_selected,
        "crew_assignments": state.crew_assignments,
        "phone_reports": [item.model_dump(mode="json") for item in state.phone_reports],
        "phone_lines": [item.model_dump(mode="json") for item in state.phone_lines],
        "phone_pending": (
            state.phone_pending.model_dump(mode="json") if state.phone_pending else None
        ),
        "dds_scenarios": [item.model_dump(mode="json") for item in state.dds_scenarios],
        "pending_dds_scenarios": [item.model_dump(mode="json") for item in state.pending_dds_scenarios],
        "operator_kio": state.operator_kio.model_dump(mode="json") if state.operator_kio else None,
        "operator_scenario": state.operator_scenario.model_dump(mode="json") if state.operator_scenario else None,
        "dds_live_cards": [_dump_live_card(item, now) for item in state.dds_live_cards],
        "dds_active_card_id": (
            str(state.dds_active_card_id) if state.dds_active_card_id else None
        ),
        "dds_card_index": state.dds_card_index,
        "dds_arrival_interval_seconds": state.dds_arrival_interval_seconds,
        "dds_max_waiting": state.dds_max_waiting,
        "dds_next_scenario_index": state.dds_next_scenario_index,
        "dds_next_arrival_at": (
            state.dds_next_arrival_at.isoformat() if state.dds_next_arrival_at else None
        ),
        "dds_completed": [
            {
                "card_id": str(item.card_id),
                "scenario_id": item.scenario_id,
                "reply_text": item.reply_text,
                "metrics": [metric.model_dump(mode="json") for metric in item.metrics],
                "findings": [finding.model_dump(mode="json") for finding in item.findings],
                "actions": item.actions,
                "duration_ms": item.duration_ms,
            }
            for item in state.dds_completed
        ],
        "reply_text": state.reply_text,
        "reply_grammar": _grammar(state.reply_grammar),
        "reply_log": [[at.isoformat(), text] for at, text in state.reply_log],
        "resolved_outcome": state.resolved_outcome,
        "resolve_comment": state.resolve_comment,
        "processed_station_commands": state.processed_station_commands[-512:],
    }
    # В actions/score могут быть datetime/UUID из расчёта; JSONB должен
    # получать только стандартные JSON-типы.
    return jsonable_encoder(payload)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _restore_timers(payload: dict, saved_at: datetime) -> SessionTimers:
    raw = payload.get("timers") or {}
    limits = {
        TimerCode(code): int(limit)
        for code, limit in (raw.get("limits") or {}).items()
    }
    restored = SessionTimers(limits=limits or SessionTimers().limits)
    now_mono = time.monotonic()
    now_wall = datetime.now(UTC)
    if saved_at.tzinfo is None:
        saved_at = saved_at.replace(tzinfo=UTC)
    downtime_ms = max(0, int((now_wall - saved_at).total_seconds() * 1000))
    for raw_code, item in (raw.get("items") or {}).items():
        code = TimerCode(raw_code)
        stopped = bool(item.get("stopped"))
        elapsed = max(0, int(item.get("elapsed_ms", 0)))
        total = elapsed if stopped else elapsed + downtime_ms
        restored.timers[code] = Timer(
            code=code,
            started_at=None if stopped else now_mono - total / 1000,
            elapsed_ms=elapsed if stopped else 0,
            attempt=max(1, int(item.get("attempt", 1))),
            stopped=stopped,
        )
    return restored


def _restore_grammar(value: dict | None) -> GrammarAssessment | None:
    if not value:
        return None
    return GrammarAssessment(
        passed=bool(value["passed"]),
        errors=tuple(value.get("errors") or []),
        source=value["source"],
    )


def _restore_live_card(item: dict, saved_at: datetime) -> DdsLiveCard:
    return DdsLiveCard(
        original_index=int(item["original_index"]),
        scenario=Scenario.model_validate(item["scenario"]),
        kio=KIO.model_validate(item["kio"]),
        dispatched_card=KIO.model_validate(item["dispatched_card"]),
        dispatched_at=datetime.fromisoformat(item["dispatched_at"]),
        timers=_restore_timers({"timers": item["timers"]}, saved_at),
        bounced_fields=list(item.get("bounced_fields") or []),
        dds_log=[(action, datetime.fromisoformat(at), detail)
                 for action, at, detail in item.get("dds_log", [])],
        status_log=[StatusEntry.model_validate(entry)
                    for entry in item.get("status_log", [])],
        crew_selected=item.get("crew_selected"),
        crew_assignments=dict(item.get("crew_assignments") or {}),
        phone_reports=[PhoneReportRecord.model_validate(entry)
                       for entry in item.get("phone_reports", [])],
        phone_lines=[PhoneLineRecord.model_validate(entry)
                     for entry in item.get("phone_lines", [])],
        phone_pending=(PhoneCallPending.model_validate(item["phone_pending"])
                       if item.get("phone_pending") else None),
        reply_text=item.get("reply_text", ""),
        reply_grammar=_restore_grammar(item.get("reply_grammar")),
        reply_log=[(datetime.fromisoformat(at), text)
                   for at, text in item.get("reply_log", [])],
    )


def load_state(payload: dict, saved_at: datetime) -> SessionState:
    """Восстановить состояние; неизвестная версия отклоняется явно."""
    if payload.get("version") != CHECKPOINT_VERSION:
        raise ValueError("неподдерживаемая версия снимка занятия")
    scenario = Scenario.model_validate(payload["scenario"]) if payload.get("scenario") else None
    state = SessionState(
        session_id=UUID(payload["session_id"]),
        scenario_id=payload["scenario_id"],
        scenario_title=payload["scenario_title"],
        level=payload["level"],
        mode=SessionMode(payload["mode"]),
        owner_login=payload.get("owner_login"),
        backend_fencing_epoch=int(payload.get("backend_fencing_epoch", 0)),
        exercise=Exercise(payload["exercise"]),
        handoff_to_dds=bool(payload.get("handoff_to_dds")),
        required_fields=list(payload.get("required_fields") or []),
        trainee_name=payload.get("trainee_name"),
        trainee_id=UUID(payload["trainee_id"]) if payload.get("trainee_id") else None,
        dds_service=payload.get("dds_service"),
        attempt=int(payload.get("attempt", 1)),
        criteria=LessonCriteria.model_validate(payload.get("criteria") or {}),
        kio=KIO.model_validate(payload.get("kio") or {}),
        transcript=[TranscriptEntry.model_validate(item) for item in payload.get("transcript", [])],
        timers=_restore_timers(payload, saved_at),
        hints_shown=list(payload.get("hints_shown") or []),
        hints_log=[(item, datetime.fromisoformat(at))
                   for item, at in payload.get("hints_log", [])],
        notes=list(payload.get("notes") or []),
        directives=list(payload.get("directives") or []),
        scenario=scenario,
        audio_frames=int(payload.get("audio_frames", 0)),
        bad_frames=int(payload.get("bad_frames", 0)),
        self_assessed=bool(payload.get("self_assessed")),
        self_assessment=payload.get("self_assessment"),
        score=payload.get("score"),
        started_at=_dt(payload.get("started_at")),
        ended_at=_dt(payload.get("ended_at")),
        end_reason=(CallEndReason(payload["end_reason"]) if payload.get("end_reason") else None),
        dispatched_card=(
            KIO.model_validate(payload["dispatched_card"])
            if payload.get("dispatched_card") else None
        ),
        dispatched_at=_dt(payload.get("dispatched_at")),
        bounced_fields=list(payload.get("bounced_fields") or []),
        dds_log=[(action, datetime.fromisoformat(at), detail)
                 for action, at, detail in payload.get("dds_log", [])],
        status_log=[StatusEntry.model_validate(item) for item in payload.get("status_log", [])],
        crew_selected=payload.get("crew_selected"),
        crew_assignments=dict(payload.get("crew_assignments") or {}),
        phone_reports=[PhoneReportRecord.model_validate(item)
                       for item in payload.get("phone_reports", [])],
        phone_lines=[PhoneLineRecord.model_validate(item)
                     for item in payload.get("phone_lines", [])],
        phone_pending=(
            PhoneCallPending.model_validate(payload["phone_pending"])
            if payload.get("phone_pending") else None
        ),
        dds_scenarios=[Scenario.model_validate(item)
                       for item in payload.get("dds_scenarios", [])],
        pending_dds_scenarios=[Scenario.model_validate(item)
                               for item in payload.get("pending_dds_scenarios", [])],
        operator_kio=(KIO.model_validate(payload["operator_kio"])
                      if payload.get("operator_kio") else None),
        operator_scenario=(Scenario.model_validate(payload["operator_scenario"])
                           if payload.get("operator_scenario") else None),
        dds_live_cards=[_restore_live_card(item, saved_at)
                        for item in payload.get("dds_live_cards", [])],
        dds_active_card_id=(
            UUID(payload["dds_active_card_id"])
            if payload.get("dds_active_card_id") else None
        ),
        dds_card_index=int(payload.get("dds_card_index", 0)),
        dds_arrival_interval_seconds=int(payload.get("dds_arrival_interval_seconds", 0)),
        dds_max_waiting=int(payload.get("dds_max_waiting", 3)),
        dds_next_scenario_index=int(payload.get(
            "dds_next_scenario_index",
            max((item["original_index"] for item in payload.get("dds_live_cards", [])), default=-1) + 1,
        )),
        dds_next_arrival_at=_dt(payload.get("dds_next_arrival_at")),
        dds_completed=[
            DdsCardRecord(
                card_id=UUID(item["card_id"]),
                scenario_id=item["scenario_id"],
                reply_text=item.get("reply_text", ""),
                metrics=[Metric.model_validate(metric) for metric in item.get("metrics", [])],
                findings=[Finding.model_validate(finding)
                          for finding in item.get("findings", [])],
                actions=list(item.get("actions") or []),
                duration_ms=int(item.get("duration_ms", 0)),
            )
            for item in payload.get("dds_completed", [])
        ],
        reply_text=payload.get("reply_text", ""),
        reply_grammar=_restore_grammar(payload.get("reply_grammar")),
        reply_log=[(datetime.fromisoformat(at), text)
                   for at, text in payload.get("reply_log", [])],
        resolved_outcome=payload.get("resolved_outcome"),
        resolve_comment=payload.get("resolve_comment", ""),
        processed_station_commands=list(payload.get("processed_station_commands") or [])[-512:],
    )
    if state.dds_live_cards:
        # Legacy snapshots had no explicit active ID; newer snapshots may
        # intentionally be between cards while waiting for the next arrival.
        active_id = state.dds_active_card_id
        if active_id is None and "dds_active_card_id" not in payload:
            active_id = state.dds_live_cards[0].card_id
        if active_id is not None:
            state.activate_dds_card(active_id, capture=False)
    if (state.dds_scenarios
            and state.dds_next_scenario_index < len(state.dds_scenarios)
            and state.dds_next_arrival_at is None):
        # Old checkpoints had no delivery schedule; resume any remaining
        # selected scenarios immediately rather than strand the session.
        state.dds_next_arrival_at = now_utc()
    # Голосовые runtime-объекты не сериализуются. Их безопасно собрать заново;
    # карточка и оценка при этом остаются прежними.
    if state.exercise is Exercise.CALL and state.scenario is not None:
        state.persona = PersonaState(state.scenario.persona)
        state.caller = build_caller(
            state.scenario.id,
            use_pregenerated=state.scenario.tree.pregenerated,
        )
        embedder = get_embedder()
        if embedder is not None:
            state.slots = SlotMachine(state.scenario, embedder)
    return state
