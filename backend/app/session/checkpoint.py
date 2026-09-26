"""Переносимый JSON-снимок незавершённого занятия.

Снимок хранится в PostgreSQL после каждого подтверждённого действия. Это
`model_dump` сохраняемой части сессии (`PersistedSession`): сокеты, аудиобуферы
и объекты моделей в неё не входят и после перезапуска процесса собираются
заново (`rebuild_live`).

Снимки прежних версий не читаются: снимок живёт только у незавершённого
занятия, а плохой снимок реестр и восстановление узла логируют и пропускают.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from app.dialog.factory import build_caller
from app.dialog.persona import PersonaProgress, PersonaState
from app.dialog.runtime import get_embedder
from app.dialog.slots import SlotMachine, SlotProgress
from app.domain.events import Exercise
from app.session.state import SessionState
from app.session.timers import downtime_ms

CHECKPOINT_VERSION = 2


class CallerProgress(BaseModel):
    """Прогресс голосового звонящего. В снимок пока не входит — см. `SessionState`."""

    slots: SlotProgress = Field(default_factory=SlotProgress)
    persona: PersonaProgress = Field(default_factory=PersonaProgress)


def dump_state(state: SessionState) -> dict:
    """Сериализовать сохраняемую часть. Чистое чтение."""
    return {"version": CHECKPOINT_VERSION, **state.model_dump(mode="json")}


def rebuild_live(state: SessionState, caller: CallerProgress | None = None) -> None:
    """Собрать runtime-объекты звонящего; без прогресса — с чистого листа.

    Карточка и оценка при этом остаются прежними.
    """
    if state.exercise is not Exercise.CALL or state.scenario is None:
        return
    progress = caller or CallerProgress()
    state.persona = PersonaState(state.scenario.persona, progress=progress.persona)
    state.caller = build_caller(
        state.scenario.id,
        use_pregenerated=state.scenario.tree.pregenerated,
    )
    embedder = get_embedder()
    if embedder is not None:
        state.slots = SlotMachine(state.scenario, embedder, progress=progress.slots)


def load_state(payload: dict, saved_at: datetime) -> SessionState:
    """Восстановить состояние; неизвестная версия отклоняется явно."""
    if payload.get("version") != CHECKPOINT_VERSION:
        raise ValueError("неподдерживаемая версия снимка занятия")
    state = SessionState.model_validate(
        payload, context={"downtime_ms": downtime_ms(saved_at)}
    )
    if state.exercise is Exercise.CALL:
        # Таймеры звонка и диспетчера — одна цепочка 112 → ДДС (см. dispatch):
        # в снимке это копии, в живой сессии — один объект.
        for card in state.desk.cards.values():
            card.timers = state.timers
    rebuild_live(state)
    return state
