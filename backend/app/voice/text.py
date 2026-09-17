"""Текст реплики → текст, который Silero произнесёт целиком.

Silero v5 **молча выбрасывает цифры и латиницу**: «улица Ленина, 14, квартира 47,
5-й этаж» звучит на 1.6 с короче того же адреса словами — номера дома, квартиры
и этажа не произносятся вовсе. Оператор при любом опросе не услышал бы адрес,
а это главный факт упражнения. Поэтому всё, что в фактах записано цифрами
и сокращениями, разворачивается в слова до синтеза.
"""

import re

from num2words import num2words

ABBREVIATIONS = {
    r"\bул\.": "улица",
    r"\bд\.": "дом",
    r"\bкв\.": "квартира",
    r"\bпр-т\b": "проспект",
    r"\bпер\.": "переулок",
    r"\bкорп\.": "корпус",
    r"\bстр\.": "строение",
    r"\bпод\.": "подъезд",
    r"\bэт\.": "этаж",
}

LATIN = {
    "a": "а", "b": "бэ", "c": "цэ", "d": "дэ", "e": "е", "f": "эф", "g": "гэ", "h": "аш",
    "i": "и", "j": "йот", "k": "ка", "l": "эль", "m": "эм", "n": "эн", "o": "о", "p": "пэ",
    "q": "ку", "r": "эр", "s": "эс", "t": "тэ", "u": "у", "v": "вэ", "w": "дубль вэ",
    "x": "икс", "y": "игрек", "z": "зэт",
}

# Окончание порядкового числительного в записи «5-й», «5-я», «5-е» задаёт род.
_ORDINAL = re.compile(r"\b(\d+)-(й|я|е|го|му|м|х)\b")
_NUMBER = re.compile(r"\d+")


def _ordinal(number: int, suffix: str) -> str:
    word = num2words(number, lang="ru", to="ordinal")  # мужской род: «пятый»
    if suffix == "я":
        return re.sub(r"(ый|ой)$", "ая", re.sub(r"ий$", "ья" if word.endswith("тий") else "яя", word))
    if suffix == "е":
        return re.sub(r"(ый|ой)$", "ое", re.sub(r"ий$", "ье" if word.endswith("тий") else "ее", word))
    return word


def normalize(text: str) -> str:
    """Вернуть текст, который синтез произнесёт без пропусков. Пустой — если говорить нечего."""
    for pattern, full in ABBREVIATIONS.items():
        text = re.sub(pattern, full, text, flags=re.IGNORECASE)

    text = _ORDINAL.sub(lambda m: _ordinal(int(m.group(1)), m.group(2)), text)
    # «47Б», «14B» — буква корпуса или квартиры прилипает к числу.
    text = re.sub(r"(\d+)([A-Za-zА-Яа-я])\b", r"\1 \2", text)
    text = _NUMBER.sub(lambda m: num2words(int(m.group()), lang="ru"), text)
    text = re.sub(r"[A-Za-z]", lambda m: f" {LATIN[m.group().lower()]} ", text)
    return re.sub(r"\s+", " ", text).strip()


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def sentences(text: str) -> list[str]:
    """Реплика по предложениям: синтез стартует с первого, не дожидаясь остальных."""
    return [part for part in (piece.strip() for piece in _SENTENCE_END.split(text)) if part]


#: Предложение, которое **звучит** длиннее этого, режется по запятым. Первый звук
#: ждёт синтеза первого куска целиком: адрес одним предложением — 4 с звука и ~450 мс
#: синтеза, первая часть «Улица Ленина, дом четырнадцать» — вдвое быстрее.
#: Длина меряется по произносимому тексту: «14» в записи — два символа, в звуке —
#: «четырнадцать».
LONG_SENTENCE = 40
MIN_CHUNK = 25


def speech_chunks(text: str) -> list[str]:
    """Куски для синтеза: предложения, а длинные — ещё и по запятым.

    Следующий кусок синтезируется, пока играет предыдущий: синтез в 10 раз быстрее
    реального времени, и стыка не слышно. Совсем короткие куски приклеиваются
    к соседним — отдельно синтезированное «дом» звучит обрывком.
    """
    chunks: list[str] = []
    for sentence in sentences(text):
        if len(normalize(sentence)) <= LONG_SENTENCE:
            chunks.append(sentence)
            continue
        parts = [part.strip() for part in re.split(r"(?<=,)\s+", sentence) if part.strip()]
        current = ""
        for index, part in enumerate(parts):
            current = f"{current} {part}".strip()
            last = index == len(parts) - 1
            # Номер не отрывается от улицы: «Ленина» / «четырнадцать» звучит как
            # два факта, и оператор расслышит улицу, но потеряет дом.
            number_follows = not last and parts[index + 1][:1].isdigit()
            if last or (len(normalize(current)) >= MIN_CHUNK and not number_follows):
                chunks.append(current)
                current = ""
    return chunks
