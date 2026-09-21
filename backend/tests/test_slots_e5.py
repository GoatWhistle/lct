"""Качество матчинга на настоящей модели multilingual-e5-small.

Фразы здесь **отложенные**: ни одна не совпадает с формулировками из
scenarios/checklists/fire.yaml и не-вопросами из common.yaml. Иначе тест
проверял бы, что модель узнаёт собственные образцы, а не живую речь.

Ошибки несимметричны, и тест это отражает:
  * ложное срабатывание — звонящий выдал адрес на «успокойтесь», главная
    механика продукта сломана. Допускается ноль;
  * промах — звонящий переспросил «что?!», оператор перефразирует.
    Для паникующего это правдоподобно, допускается немного.

Без скачанной модели тест пропускается: `make models`.
"""

from pathlib import Path

import pytest

from app.dialog.slots import SlotMachine
from app.scenarios.loader import load_file

MODEL = Path(__file__).resolve().parents[1] / "models" / "e5-small"
LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"

pytestmark = pytest.mark.skipif(
    not (MODEL / "model_quantized.onnx").exists(), reason="нет модели эмбеддингов — make models"
)

QUESTIONS = {
    "q_address": ["Назовите адрес", "Улица, дом?", "Этаж какой?", "А этаж", "Куда ехать?", "Скажите точный адрес"],
    "q_what_burns": ["Что у вас горит?", "Горит квартира или балкон?", "Что загорелось?"],
    "q_people": ["Люди в квартире есть?", "Дети внутри есть?", "Кто-нибудь ещё в квартире?"],
    "q_smoke": ["В подъезде дым?", "Дым сильный?", "Задымление есть?"],
    "q_gas": ["Газ перекрыть можете?", "Плита газовая?"],
    "q_caller": ["Представьтесь, пожалуйста", "Как к вам обращаться?", "Оставьте номер для связи"],
}

NOT_QUESTIONS = [
    "Служба 112, слушаю",
    "Я вас слышу",
    "Бригада уже выехала",
    "Говорите медленнее",
    "Понял вас",
    "Закройте дверь",
    "Сохраняйте спокойствие",
    "Всё будет хорошо",
]

#: Сколько вопросов из отложенного набора обязано распознаваться.
MIN_RECALL = 0.9


@pytest.fixture(scope="module")
def slots():
    from app.dialog.embeddings import E5Embedder

    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    return SlotMachine(scenario, E5Embedder(MODEL))


def test_no_answer_to_what_was_not_asked(slots):
    """Ноль ложных срабатываний: иначе звонящий выдаёт данные сам."""
    false_hits = {
        phrase: slots.hear(phrase).matched for phrase in NOT_QUESTIONS if slots.hear(phrase).matched
    }
    assert not false_hits, f"звонящий «услышал» вопрос там, где его не было: {false_hits}"


def test_real_questions_are_understood(slots):
    total = sum(len(phrases) for phrases in QUESTIONS.values())
    misses = []
    for expected, phrases in QUESTIONS.items():
        for phrase in phrases:
            matched = slots.hear(phrase).matched
            if matched != [expected]:
                misses.append(f"«{phrase}» → {matched or 'не понято'}, ждали {expected}")

    recall = (total - len(misses)) / total
    assert recall >= MIN_RECALL, (
        f"распознано {total - len(misses)} из {total} ({recall:.0%}), нужно {MIN_RECALL:.0%}:\n  "
        + "\n  ".join(misses)
    )


def test_one_phrase_is_fast_enough(slots):
    """Бюджет из docs/arch/STACK.md — около 10 мс: модель делит процессор
    с распознаванием и синтезом речи."""
    import time

    slots.hear("разогрев")
    started = time.monotonic()
    for _ in range(10):
        slots.hear("На каком этаже пожар?")
    per_phrase_ms = (time.monotonic() - started) * 100
    assert per_phrase_ms < 50, f"{per_phrase_ms:.0f} мс на реплику"
