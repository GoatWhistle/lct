"""Офлайн-звонящий по предгенерированной таблице. Сети не требует."""

from pathlib import Path

import pytest
import yaml

from app.dialog.persona import PersonaState
from app.dialog.slots import SlotMachine
from app.domain.events import Mood
from tests.test_slots import SCENARIO, StemEmbedder

TABLE = {
    "scenario": SCENARIO.id,
    "first_line": "Алло! Горим!",
    "reveal": {
        "f_address": {"panic": "Ленина четырнадцать, сорок седьмая квартира! Быстрее!",
                      "aggressive": "ЛЕНИНА ЧЕТЫРНАДЦАТЬ! Записали?!"},
        "f_people": {"panic": "Жена с ребёнком там, не выходят!"},
    },
    "repeat": {
        "f_address": {"panic": "Я же сказал — Ленина четырнадцать!",
                      "aggressive": "СКОЛЬКО МОЖНО! ЛЕНИНА ЧЕТЫРНАДЦАТЬ!"},
    },
    "fillers": {"panic": ["Что?! Не слышу!", "Алло! Вы там?!"]},
}


@pytest.fixture
def caller(tmp_path, monkeypatch):
    from app.dialog import tree

    monkeypatch.setattr(tree, "LIBRARY", tmp_path)
    (tmp_path / f"{SCENARIO.id}.yaml").write_text(
        yaml.safe_dump(TABLE, allow_unicode=True), encoding="utf-8"
    )
    return tree.TreeCaller(SCENARIO.id)


@pytest.fixture
def slots():
    return SlotMachine(SCENARIO, StemEmbedder(), floor=0.5)


async def test_reply_comes_from_the_table(caller, slots):
    persona = PersonaState(SCENARIO.persona)
    line = await caller.reply(slots.hear("Какой адрес?"), persona, slots)
    assert line.text == TABLE["reveal"]["f_address"]["panic"]
    assert caller.misses == 0


async def test_repeat_uses_the_irritated_line(caller, slots):
    persona = PersonaState(SCENARIO.persona)
    await caller.reply(slots.hear("Какой адрес?"), persona, slots)
    line = await caller.reply(slots.hear("Повторите адрес"), persona, slots)
    assert line.text == TABLE["repeat"]["f_address"]["panic"]


async def test_mood_picks_another_line(caller, slots):
    """Дуга и директивы переключают вариант, а не текст."""
    persona = PersonaState(SCENARIO.persona)
    persona.directive = "turns_aggressive"
    line = await caller.reply(slots.hear("Какой адрес?"), persona, slots)
    assert line.mood is Mood.AGGRESSIVE
    assert line.text == TABLE["reveal"]["f_address"]["aggressive"]


async def test_unknown_question_gets_a_filler(caller, slots):
    persona = PersonaState(SCENARIO.persona)
    line = await caller.reply(slots.hear("Вы в безопасности?"), persona, slots)
    assert line.text in TABLE["fillers"]["panic"]


async def test_hole_in_the_table_falls_back_and_is_counted(caller, slots):
    """Пропуск не оставляет звонящего без голоса, но виден по счётчику:
    предгенерацию пора повторить."""
    persona = PersonaState(SCENARIO.persona)
    line = await caller.reply(slots.hear("Что именно горит?"), persona, slots)
    assert line.text, "звонящий промолчал"
    assert caller.misses == 1


async def test_caller_never_speaks_an_unrevealed_fact(caller, slots):
    persona = PersonaState(SCENARIO.persona)
    for question in ("Служба 112, слушаю", "Успокойтесь", "Вы одна дома?"):
        text = (await caller.reply(slots.hear(question), persona, slots)).text
        revealed = {fact.id for fact in slots.revealed_facts()}
        for fact in SCENARIO.facts:
            if fact.id not in revealed:
                assert fact.value not in text, f"выдал «{fact.value}» на «{question}»"
