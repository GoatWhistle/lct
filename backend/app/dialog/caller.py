"""Реплика звонящего.

`TemplateCaller` работает без LLM: говорит только раскрытыми фактами и заготовками
по настроению. Это и офлайн-запасной путь, и режим `make repl`, пока нет ключа.
Звонящий на LLM встанет за тот же интерфейс `Caller`.

70% восприятия эмоции даёт текст: обрывки, повторы, незаконченные фразы.
Заготовки написаны так же — «алло! алло!», а не «я взволнован».
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from app.dialog.persona import PersonaState
from app.dialog.slots import SlotMachine, TurnResult
from app.domain.events import Mood

log = logging.getLogger(__name__)


@dataclass
class CallerLine:
    text: str
    mood: Mood


class Caller(Protocol):
    """Звонящий. Асинхронный: на LLM он ходит в сеть, на заготовках — нет,
    но интерфейс один, и подменяются они друг другом без правок контура."""

    async def reply(self, turn: TurnResult, persona: PersonaState, slots: SlotMachine) -> CallerLine: ...


#: Реплика, когда оператор спросил не то или непонятно. Выбор по номеру реплики,
#: а не случайный: сценарий занятия должен звучать одинаково у каждой группы.
FILLERS: dict[Mood, list[str]] = {
    Mood.PANIC: [
        "Алло?! Вы меня слышите?! Помогите быстрее!",
        "Что?! Я не понимаю! Приезжайте!",
        "Господи... быстрее, пожалуйста!",
    ],
    Mood.WORRIED: [
        "Простите, я не поняла вопрос...",
        "Что именно вам сказать?",
    ],
    Mood.CALM: [
        "Уточните, пожалуйста, что вас интересует.",
        "Не понял вопрос.",
    ],
    Mood.AGGRESSIVE: [
        "Да вы издеваетесь?! Хватит болтать, высылайте!",
        "Сколько можно?! Люди горят!",
    ],
    Mood.CONFUSED: [
        "А? Кто это... что вы говорите?",
        "Подождите... я забыл, что хотел...",
    ],
}

#: Как звонящий подаёт раскрытый факт.
REVEAL: dict[Mood, str] = {
    Mood.PANIC: "{fact}! Быстрее!",
    Mood.WORRIED: "{fact}.",
    Mood.CALM: "{fact}.",
    Mood.AGGRESSIVE: "{fact}! Записали?!",
    Mood.CONFUSED: "Так... {fact}... кажется.",
}

#: Повторный вопрос по уже сказанному.
REPEAT: dict[Mood, str] = {
    Mood.PANIC: "Я же сказал — {fact}! Записывайте!",
    Mood.WORRIED: "Я ведь уже говорила: {fact}.",
    Mood.CALM: "Я уже сказал: {fact}.",
    Mood.AGGRESSIVE: "Я ТРЕТИЙ РАЗ ГОВОРЮ — {fact}! Вы слушаете вообще?!",
    Mood.CONFUSED: "Так я ж говорил... {fact}...",
}


class TemplateCaller:
    def __init__(self) -> None:
        self._turn = 0

    async def reply(self, turn: TurnResult, persona: PersonaState, slots: SlotMachine) -> CallerLine:
        self._turn += 1
        if turn.repeated:
            for _ in turn.repeated:
                persona.on_repeat()

        mood = persona.remember()
        facts = {fact.id: fact.value for fact in slots.revealed_facts()}

        parts: list[str] = []
        for fact_id in turn.revealed:
            parts.append(REVEAL[mood].format(fact=facts[fact_id]))
        for fact_id in turn.repeated:
            parts.append(REPEAT[mood].format(fact=facts[fact_id]))

        if not parts:
            options = FILLERS[mood]
            parts.append(options[(self._turn - 1) % len(options)])

        return CallerLine(text=" ".join(_sentence_case(part) for part in parts), mood=mood)


def _sentence_case(text: str) -> str:
    """Факт в сценарии записан как фрагмент («улица Ленина, 14»), а в начале
    реплики должен звучать как начало фразы."""
    return text[:1].upper() + text[1:] if text else text


class LlmCaller:
    """Звонящий, говорящий своими словами.

    Что можно сказать, решает слот-автомат, а не модель: в промпт попадают только
    раскрытые факты. Иначе LLM услужливо назовёт адрес, которого не спрашивали
    (docs/product/CALL-SIM.md).

    Отказ сети или провайдера не роняет занятие: звонящий откатывается
    на заготовки — молчащий звонящий хуже шаблонной фразы.
    """

    def __init__(self, client, model: str, temperature: float = 0.8) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self._fallback = TemplateCaller()
        self._history: list[dict] = []
        self.fallbacks = 0

    async def reply(self, turn: TurnResult, persona: PersonaState, slots: SlotMachine) -> CallerLine:
        from app.dialog.llm import LlmRequest, LlmUnavailable

        if turn.repeated:
            for _ in turn.repeated:
                persona.on_repeat()
        mood = persona.remember()

        facts = {fact.id: fact.value for fact in slots.revealed_facts()}
        say_now = [facts[fact_id] for fact_id in turn.revealed if fact_id in facts]
        repeated = [facts[fact_id] for fact_id in turn.repeated if fact_id in facts]

        system = _prompt("caller.md").format(
            scenario=slots.scenario.title,
            mood=MOOD_WORDS.get(mood, mood.value),
            directive=_directive_line(persona),
            revealed="\n".join(f"- {value}" for value in facts.values()) or "- пока ничего",
            say_now="\n".join(f"- {value}" for value in say_now)
            or ("- ничего нового: оператор спросил не о том" if not repeated else ""),
        )
        if repeated:
            system += "\n\n" + _prompt("repeat.md").format(
                repeated="; ".join(repeated), repeats=persona.repeats
            )

        self._history.append({"role": "user", "content": turn.text})
        try:
            text = await self._client.complete(
                LlmRequest(
                    messages=[{"role": "system", "content": system}, *self._history[-6:]],
                    model=self._model,
                    temperature=self._temperature,
                )
            )
        except LlmUnavailable as exc:
            self.fallbacks += 1
            log.warning("звонящий на заготовках: %s", exc)
            return await self._fallback.reply(turn, persona, slots)

        self._history.append({"role": "assistant", "content": text})
        return CallerLine(text=text, mood=mood)

    async def aclose(self) -> None:
        """Сетевой клиент живёт, пока идёт занятие, и закрывается вместе с ним:
        незакрытый держит событийный цикл и не даёт процессу завершиться."""
        await self._client.aclose()


MOOD_WORDS = {
    Mood.PANIC: "паника, ты кричишь",
    Mood.AGGRESSIVE: "злость, ты срываешься на оператора",
    Mood.WORRIED: "тревога, ты растерян",
    Mood.CALM: "спокойствие, ты собран",
    Mood.CONFUSED: "растерянность, ты путаешься",
}


def _directive_line(persona: PersonaState) -> str:
    from app.dialog.director import SOFT

    if persona.directive in SOFT:
        return f"ПРЕПОДАВАТЕЛЬ ВЕДЁТ СИТУАЦИЮ: {SOFT[persona.directive].lower()}."
    return ""


@lru_cache(maxsize=8)
def _prompt(name: str) -> str:
    """Промпты лежат файлами, а не в коде: их правит тот, кто ведёт занятия."""
    return (Path(__file__).parent / "prompts" / name).read_text(encoding="utf-8")
