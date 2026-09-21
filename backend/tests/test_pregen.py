"""Предгенерация офлайн-таблиц: разбор ответа и планирование работы.

Сеть здесь не нужна и не используется: проверяется то, что ломается тихо —
разбор JSON от рассуждающей модели и решение «что ещё осталось сгенерировать».
Сама генерация запускается руками там, где есть провайдер (tasks/lct-19).
"""

from pathlib import Path

import pytest

from app.scenarios.loader import load_file
from scripts.pregenerate import parse, tasks_for

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"


# ── разбор ответа модели ──


def test_plain_json_is_parsed():
    assert parse('{"panic": "Горим!", "calm": "У нас пожар."}') == {
        "panic": "Горим!",
        "calm": "У нас пожар.",
    }


def test_json_in_markdown_fence_is_parsed():
    """Рассуждающие модели любят обернуть ответ в ограду."""
    answer = 'Вот реплики:\n```json\n{"panic": "Горим!"}\n```\nГотово.'
    assert parse(answer) == {"panic": "Горим!"}


def test_broken_json_gives_nothing_not_a_crash():
    """Пустой разбор — это повтор запроса, а не падение прогона на середине."""
    assert parse("извините, не могу") == {}
    assert parse('{"panic": ') == {}


def test_empty_lines_are_dropped():
    assert parse('{"panic": "   ", "calm": "Пожар."}') == {"calm": "Пожар."}


def test_non_string_values_are_dropped():
    assert parse('{"panic": ["а", "б"], "calm": "Пожар."}') == {"calm": "Пожар."}


# ── планирование ──


@pytest.fixture(scope="module")
def scenario():
    return load_file(LIBRARY / "tickets" / "t03-1-fire-private-house.yaml", LIBRARY)


def test_empty_table_needs_every_fact(scenario):
    todo = tasks_for(scenario, {}, force=False)
    sections = {section for section, _, _ in todo}
    assert "reveal" in sections and "repeat" in sections and "fillers" in sections


def test_refinable_fact_gets_its_own_section(scenario):
    """Без секции `refine` офлайн-звонящий на уточняющий вопрос ответит
    заготовкой и уточнённого адреса не назовёт (lct-35)."""
    todo = tasks_for(scenario, {}, force=False)
    refine = [key for section, key, _ in todo if section == "refine"]
    assert refine == ["f_address"]


def test_fact_without_refinement_has_no_refine_task(scenario):
    todo = tasks_for(scenario, {}, force=False)
    refine = {key for section, key, _ in todo if section == "refine"}
    assert "f_what_burns" not in refine


def test_filled_table_needs_nothing(scenario):
    """Прерванный прогон продолжается с места остановки, а не с начала."""
    from app.dialog.caller import MOOD_WORDS

    moods = {mood.value: "реплика" for mood in MOOD_WORDS}
    table = {
        "reveal": {fact.id: dict(moods) for fact in scenario.facts},
        "repeat": {fact.id: dict(moods) for fact in scenario.facts},
        "refine": {fact.id: dict(moods) for fact in scenario.facts if fact.refined},
        "fillers": {mood.value: ["раз", "два", "три"] for mood in MOOD_WORDS},
    }
    assert tasks_for(scenario, table, force=False) == []


def test_partially_filled_table_asks_only_for_the_rest(scenario):
    from app.dialog.caller import MOOD_WORDS

    moods = {mood.value: "реплика" for mood in MOOD_WORDS}
    table = {"reveal": {fact.id: dict(moods) for fact in scenario.facts}}
    sections = {section for section, _, _ in tasks_for(scenario, table, force=False)}
    assert "reveal" not in sections
    assert "repeat" in sections


def test_missing_single_mood_reopens_the_block(scenario):
    """Блок считается готовым только когда есть все настроения: иначе
    звонящий молчит ровно в том состоянии, которого не хватает."""
    from app.dialog.caller import MOOD_WORDS

    moods = {mood.value: "реплика" for mood in MOOD_WORDS}
    moods.pop("aggressive")
    table = {"reveal": {fact.id: dict(moods) for fact in scenario.facts}}
    reveal = [key for section, key, _ in tasks_for(scenario, table, force=False) if section == "reveal"]
    assert reveal, "неполный блок не попал в работу"


def test_force_regenerates_everything(scenario):
    from app.dialog.caller import MOOD_WORDS

    moods = {mood.value: "реплика" for mood in MOOD_WORDS}
    table = {
        "reveal": {fact.id: dict(moods) for fact in scenario.facts},
        "repeat": {fact.id: dict(moods) for fact in scenario.facts},
        "refine": {fact.id: dict(moods) for fact in scenario.facts if fact.refined},
        "fillers": {mood.value: ["раз", "два", "три"] for mood in MOOD_WORDS},
    }
    assert tasks_for(scenario, table, force=True)
