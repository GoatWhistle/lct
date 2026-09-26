"""Такт таймеров доходит до АРМ ДДС: пульт строит срок решения по снимку сервера."""

import asyncio
from pathlib import Path
from uuid import uuid4

from app.domain.events import Exercise, SessionMode, TimerTick
from app.domain.timers import TimerCode
from app.scenarios.loader import load_file
from app.session import hub as hub_module
from app.session.hub import SessionHub
from app.session.state import SessionState

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


async def test_station_receives_dds_ack_with_configured_limit_after_handoff(monkeypatch):
    """Передача из 112 без очереди ДДС: `queue_cards` пуст, и лимит решения,
    настроенный преподавателем, пульт видит только в `timer.tick`."""
    monkeypatch.setattr(hub_module, "TICK_SECONDS", 0.01)
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING,
        exercise=Exercise.CARD, handoff_to_dds=True, scenario=scenario,
    )
    state.timers.limits[TimerCode.DDS_ACK] = 45_000
    state.on_event("dds.dispatch")
    state.dispatch()
    local_hub = SessionHub()
    local_hub.register(state)

    with local_hub.station(state.session_id) as station:
        local_hub.start_ticker(state.session_id)
        try:
            async with asyncio.timeout(1):
                while not isinstance(event := await station.get(), TimerTick):
                    pass
        finally:
            local_hub.stop_ticker(state.session_id)

    ack = next(timer for timer in event.timers if timer.code is TimerCode.DDS_ACK)
    assert ack.limit_ms == 45_000
    assert not ack.stopped
