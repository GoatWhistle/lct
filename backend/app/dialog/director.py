"""Директивы преподавателя: он ведёт ситуацию по ходу разговора.

«Управление ситуаций с рабочего места преподавателя» — дословное требование ТЗ,
понятое буквально (docs/product/CALL-SIM.md#преподаватель-режиссёр).

Мягкие директивы меняют подачу и живут до конца звонка, жёсткие правят факты
и таймеры. **Ни одна не трогает карточку курсанта:** преподаватель управляет
ситуацией, а не работой обучаемого.
"""

from dataclasses import dataclass

from app.domain.events import Mood

#: Мягкие директивы: только подача. Настроение берёт `PersonaState`.
SOFT = {
    "panic_rises": "Паника нарастает",
    "screaming": "Переходит на крик",
    "turns_aggressive": "Переходит в агрессию",
    "distracted": "Звонящий отвлёкся",
}

#: Жёсткие: правят факты, таймеры или ход звонка.
HARD = {
    "line_dropped": "Связь обрывается",
    "second_victim": "Появился второй пострадавший",
    "address_wrong": "Адрес оказался неточным",
}

#: Что звонящий скажет следующей репликой по жёсткой директиве.
HARD_LINES = {
    "second_victim": "Тут ещё женщина на площадке! Она не дышит! Быстрее!",
    "address_wrong": "Стойте! Я перепутал, адрес другой! Запишите заново!",
}


@dataclass
class DirectiveResult:
    applied: bool
    #: Реплика, которую звонящий должен сказать сразу.
    say: str | None = None
    #: Оборвать связь: звук на полуслове, дальше обратный дозвон.
    drop_line: bool = False
    #: Директива требует локально работающую модель (без неё — только кнопки).
    needs_network: bool = False


def apply(state, directive: str) -> DirectiveResult:
    """Применить директиву к живому занятию."""
    if directive in SOFT:
        if state.persona is not None:
            state.persona.directive = directive
        return DirectiveResult(applied=True)

    if directive == "line_dropped":
        return DirectiveResult(applied=True, drop_line=True)

    if directive == "second_victim":
        # Правка эталона: теперь пострадавших на одного больше, и оценка
        # карточки считается против нового числа.
        truth = state.scenario.ground_truth if state.scenario else None
        if truth is not None:
            truth.victims = (truth.victims or 0) + 1
        return DirectiveResult(applied=True, say=HARD_LINES[directive])

    if directive == "address_wrong":
        # Факт снимается с раскрытых: оператор обязан переспросить адрес,
        # а в оценке полнота опроса снова считает его недобытым.
        if state.slots is not None:
            for fact in list(state.slots.revealed_facts()):
                if "address" in fact.id:
                    state.slots.invalidate(fact.id)
        return DirectiveResult(applied=True, say=HARD_LINES[directive])

    # Свободный текст разрешён лишь в режиме локальной модели. Он управляет
    # интонацией, но не раскрывает факты и не меняет правила 112.
    from app.dialog.caller import LlmCaller

    if not isinstance(state.caller, LlmCaller) or len(directive) > 500:
        return DirectiveResult(applied=False, needs_network=True)
    if state.persona is not None:
        state.persona.directive = directive
    return DirectiveResult(applied=True)


def mood_of(state) -> Mood:
    return state.persona.mood if state.persona else Mood.PANIC
