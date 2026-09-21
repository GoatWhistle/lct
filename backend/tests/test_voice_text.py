"""Нормализация текста перед синтезом: адрес должен прозвучать целиком."""

import re

from app.voice.text import normalize, sentences, speech_chunks


def test_address_digits_become_words():
    spoken = normalize("улица Ленина, 14, квартира 47, 5-й этаж")
    assert not re.search(r"\d", spoken), spoken
    assert "четырнадцать" in spoken and "сорок семь" in spoken and "пятый" in spoken


def test_abbreviations_are_expanded():
    assert normalize("ул. Ленина, д. 14, кв. 47") == "улица Ленина, дом четырнадцать, квартира сорок семь"


def test_ordinal_gender_follows_suffix():
    assert normalize("5-я линия") == "пятая линия"
    assert normalize("3-е окно") == "третье окно"
    assert normalize("2-й подъезд") == "второй подъезд"


def test_building_letter_is_spoken():
    spoken = normalize("дом 47B")
    assert "сорок семь" in spoken and "бэ" in spoken and "B" not in spoken


def test_nothing_to_say_is_empty_not_an_error():
    """Пустая строка роняет Silero ValueError — нормализация отдаёт пустое,
    и синтез просто не вызывается."""
    assert normalize("   ") == ""


def test_reply_splits_by_sentence():
    assert sentences("Алло! Горим! Улица Ленина, дом четырнадцать.") == [
        "Алло!", "Горим!", "Улица Ленина, дом четырнадцать."
    ]


def test_long_address_is_chunked_for_a_fast_first_sound():
    chunks = speech_chunks("Улица Ленина, 14, квартира 47, 5-й этаж! Быстрее!")
    assert chunks[0] == "Улица Ленина, 14,", chunks
    assert "".join(chunks).replace(" ", "") == "УлицаЛенина,14,квартира47,5-йэтаж!Быстрее!"


def test_short_sentences_are_not_chopped():
    assert speech_chunks("Алло! Горим!") == ["Алло!", "Горим!"]


def test_house_number_stays_with_the_street():
    chunks = speech_chunks("Я же сказал — улица Ленина, 14, квартира 47, 5-й этаж! Записывайте!")
    assert chunks[0].endswith("Ленина, 14,"), chunks
