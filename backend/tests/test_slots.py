"""Слот-автомат и звонящий без LLM.

Логика проверяется на детерминированной заглушке эмбеддингов: тесты не зависят
от скачанной модели. Качество матчинга на настоящей e5 — в test_slots_e5.py.
"""

import re
import zlib

import numpy as np
import pytest

from app.dialog.caller import TemplateCaller
from app.dialog.persona import PersonaState
from app.dialog.slots import SlotMachine
from app.domain.events import Mood
from app.scenarios.schema import Scenario


class StemEmbedder:
    """Мешок основ слов: пять первых букв, хеш в вектор. Грубо, но
    детерминированно — «этаже» и «этаж» совпадают, «пожар» и «адрес» нет.

    Служебные слова выброшены: без этого «Повторите адрес» и «Какой адрес
    и этаж» расходятся на вопросительных словах, а не сходятся на адресе.
    """

    DIM = 512
    STOP = {"что", "как", "какой", "какая", "есть", "вас", "это", "ещё", "раз", "повто", "уточн"}

    def embed(self, texts):
        rows = []
        for text in texts:
            vector = np.zeros(self.DIM)
            for word in re.findall(r"\w+", text.lower()):
                stem = word[:5]
                if len(word) > 2 and word not in self.STOP and stem not in self.STOP:
                    vector[zlib.crc32(stem.encode()) % self.DIM] += 1.0
            norm = np.linalg.norm(vector)
            rows.append(vector / norm if norm else vector)
        return np.array(rows)


SCENARIO = Scenario.model_validate(
    {
        "id": "slots-test",
        "title": "Проверка автомата",
        "type": "fire",
        "level": "L2",
        "persona": {"base": "panic", "arc": [{"stage": "registration", "mood": "panic"}]},
        "first_line": "Алло! Горим!",
        "facts": [
            {"id": "f_address", "value": "улица Ленина, 14, 5-й этаж", "reveal_on": {"question": "q_address"}},
            {"id": "f_people", "value": "в квартире жена и ребёнок", "reveal_on": {"question": "q_people"}},
            {"id": "f_burns", "value": "горит балкон", "reveal_on": {"question": "q_burns"}},
            {
                "id": "f_secret",
                "value": "муж курил на балконе",
                "hidden": True,
                "reveal_on": {"approach": "объяснил, что вину никто не ищет"},
            },
        ],
        "checklist": [
            {"id": "q_address", "question": "Какой адрес и этаж", "fact": "f_address"},
            {"id": "q_people", "question": "Есть ли люди в квартире", "fact": "f_people"},
            {"id": "q_burns", "question": "Что именно горит", "fact": "f_burns"},
            {"id": "q_cause", "question": "Из-за чего начался пожар", "fact": "f_secret"},
        ],
        "ground_truth": {"required_facts": ["f_address", "f_people", "f_burns"]},
    }
)


@pytest.fixture
def slots():
    return SlotMachine(SCENARIO, StemEmbedder(), floor=0.5)


def test_nothing_is_revealed_without_a_question(slots):
    turn = slots.hear("Служба 112, что у вас случилось?")
    assert turn.revealed == []
    assert slots.revealed_facts() == []


def test_question_reveals_its_fact(slots):
    turn = slots.hear("Назовите адрес и этаж")
    assert turn.revealed == ["f_address"]
    assert "q_address" in slots.asked


def test_repeat_is_detected_not_revealed_again(slots):
    slots.hear("Какой адрес?")
    turn = slots.hear("Повторите адрес")
    assert turn.revealed == []
    assert turn.repeated == ["f_address"], "повтор не распознан — звонящий не разозлится"


def test_two_questions_in_one_line_both_count(slots):
    turn = slots.hear("Какой адрес? Есть ли люди в квартире?")
    assert set(turn.revealed) == {"f_address", "f_people"}


def test_hidden_fact_is_not_given_for_a_direct_question(slots):
    """Скрывающий звонящий уклоняется от прямого вопроса: факт раскрывается
    только подходом, иначе механика L3 превращается в обычный чек-лист."""
    turn = slots.hear("Из-за чего начался пожар?")
    assert "f_secret" not in turn.revealed
    assert "q_cause" in slots.asked, "вопрос задан — это должно быть видно в разборе"

    assert slots.reveal_by_approach("f_secret")
    assert "f_secret" in [fact.id for fact in slots.revealed_facts()]


def test_unasked_shrinks_in_checklist_order(slots):
    assert [item.id for item in slots.unasked()][:2] == ["q_address", "q_people"]
    slots.hear("Какой адрес?")
    assert slots.unasked()[0].id == "q_people", "подсказка должна вести к следующему пункту"


def test_missing_required_is_the_basis_for_e1(slots):
    slots.hear("Какой адрес?")
    assert slots.missing_required() == ["f_people", "f_burns"]


def test_invalidated_fact_must_be_asked_again(slots):
    """Директива «адрес оказался неточным»."""
    slots.hear("Какой адрес?")
    slots.invalidate("f_address")
    assert slots.hear("Уточните адрес").revealed == ["f_address"]


def test_caller_never_speaks_an_unrevealed_fact(slots):
    """Ключевое свойство продукта: звонящий не выдаёт данные сам.
    Проверяется по всем фактам на длинной серии реплик, включая мимо чек-листа."""
    caller = TemplateCaller()
    persona = PersonaState(SCENARIO.persona)
    lines = [
        "Служба 112, слушаю",
        "Успокойтесь, пожалуйста",
        "Что случилось?",
        "Какой адрес?",
        "Вы в безопасности?",
        "Говорите громче",
        "Есть ли люди в квартире?",
        "Из-за чего начался пожар?",
    ]
    for line in lines:
        turn = slots.hear(line)
        reply = caller.reply(turn, persona, slots).text
        revealed = {fact.id for fact in slots.revealed_facts()}
        for fact in SCENARIO.facts:
            if fact.id not in revealed:
                assert fact.value not in reply, f"звонящий выдал «{fact.value}» без вопроса на «{line}»"


def test_repeats_push_the_caller_into_aggression(slots):
    caller = TemplateCaller()
    persona = PersonaState(SCENARIO.persona)

    first = caller.reply(slots.hear("Какой адрес?"), persona, slots)
    assert first.mood is Mood.PANIC

    caller.reply(slots.hear("Адрес какой?"), persona, slots)
    third = caller.reply(slots.hear("Ещё раз адрес"), persona, slots)
    assert third.mood is Mood.AGGRESSIVE, "настойчивый повтор должен сдвигать к агрессии"
    assert "Ленина" in third.text, "в раздражении звонящий всё равно повторяет факт"


def test_directive_overrides_the_arc():
    persona = PersonaState(SCENARIO.persona)
    persona.directive = "turns_aggressive"
    assert persona.mood is Mood.AGGRESSIVE


def test_same_lines_give_same_replies():
    """Сценарий занятия должен звучать одинаково у каждой группы."""
    def run():
        machine = SlotMachine(SCENARIO, StemEmbedder(), floor=0.5)
        caller, persona = TemplateCaller(), PersonaState(SCENARIO.persona)
        return [caller.reply(machine.hear(line), persona, machine).text for line in ("Алло", "Какой адрес?", "Что?")]

    assert run() == run()
