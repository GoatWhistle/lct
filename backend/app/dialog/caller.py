"""Реплика звонящего.

`TemplateCaller` работает без LLM: говорит только раскрытыми фактами и заготовками
по настроению. Это и офлайн-запасной путь, и режим `make repl`, пока нет ключа.
Звонящий на LLM встанет за тот же интерфейс `Caller`.

70% восприятия эмоции даёт текст: обрывки, повторы, незаконченные фразы.
Заготовки написаны так же — «алло! алло!», а не «я взволнован».
"""

from dataclasses import dataclass
from typing import Protocol

from app.dialog.persona import PersonaState
from app.dialog.slots import SlotMachine, TurnResult
from app.domain.events import Mood


@dataclass
class CallerLine:
    text: str
    mood: Mood


class Caller(Protocol):
    def reply(self, turn: TurnResult, persona: PersonaState, slots: SlotMachine) -> CallerLine: ...


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

    def reply(self, turn: TurnResult, persona: PersonaState, slots: SlotMachine) -> CallerLine:
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
