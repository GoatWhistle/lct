"""Офлайн-звонящий: читает таблицу реплик с диска.

Ветвление делает слот-автомат, поэтому таблица индексируется парой
«событие × настроение», а не хранит граф диалога. На занятии сети не нужно
вовсе: задержка — это время матчинга эмбеддингами, около 300 мс.

Это не запасной костыль, а требование воспроизводимости: сценарий занятия
проверяется методистом заранее и звучит одинаково у каждой группы
(docs/arch/STACK.md).
"""

import logging
from pathlib import Path

import yaml

from app.dialog.caller import CallerLine, TemplateCaller
from app.dialog.persona import PersonaState
from app.dialog.slots import SlotMachine, TurnResult
from app.domain.events import Mood

log = logging.getLogger(__name__)

LIBRARY = Path(__file__).resolve().parents[3] / "scenarios" / "pregenerated"


def table_path(scenario_id: str) -> Path:
    return LIBRARY / f"{scenario_id}.yaml"


def has_table(scenario_id: str) -> bool:
    return table_path(scenario_id).exists()


class TreeCaller:
    """Звонящий по предгенерированной таблице.

    Чего в таблице нет — берётся у заготовок: пропуск не должен оставлять
    звонящего без голоса посреди занятия.
    """

    def __init__(self, scenario_id: str) -> None:
        self._table = yaml.safe_load(table_path(scenario_id).read_text(encoding="utf-8")) or {}
        self._fallback = TemplateCaller()
        self._turn = 0
        self.misses = 0

    @property
    def first_line(self) -> str | None:
        return self._table.get("first_line")

    def _line(self, section: str, fact_id: str, mood: Mood) -> str | None:
        return (self._table.get(section, {}).get(fact_id) or {}).get(mood.value)

    def _filler(self, mood: Mood) -> str | None:
        options = self._table.get("fillers", {}).get(mood.value) or []
        return options[(self._turn - 1) % len(options)] if options else None

    async def reply(self, turn: TurnResult, persona: PersonaState, slots: SlotMachine) -> CallerLine:
        self._turn += 1
        for _ in turn.repeated:
            persona.on_repeat()
        mood = persona.remember()

        parts: list[str] = []
        for fact_id in turn.revealed:
            parts.append(self._line("reveal", fact_id, mood) or "")
        for fact_id in turn.repeated:
            parts.append(self._line("repeat", fact_id, mood) or "")

        if parts and all(parts):
            return CallerLine(text=" ".join(parts), mood=mood)
        if not parts:
            filler = self._filler(mood)
            if filler:
                return CallerLine(text=filler, mood=mood)

        # В таблице дырка: реплику берём у заготовок, но считаем — по счётчику
        # видно, что предгенерацию пора повторить.
        self.misses += 1
        log.warning("в таблице нет реплики (%s), звонящий отвечает заготовкой", mood.value)
        return await self._fallback.reply(turn, persona, slots)
