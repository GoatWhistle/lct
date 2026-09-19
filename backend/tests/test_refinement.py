"""Уточнение адреса: место происшествия ≠ адрес заявителя.

Пункты приёмки lct-35. Логика — на детерминированной заглушке эмбеддингов;
что живая модель отличает уточняющий вопрос от обычного, проверяется отдельно
и пропускается без скачанной модели.
"""

from pathlib import Path

import pytest

from app.dialog.caller import TemplateCaller
from app.dialog.persona import PersonaState
from app.dialog.slots import SlotMachine
from app.scenarios.loader import load_file
from app.scenarios.schema import Scenario
from app.scoring.gost import evaluate
from app.session.timers import SessionTimers
from tests.test_slots import StemEmbedder

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"
MODEL = Path(__file__).resolve().parents[1] / "models" / "e5-small"

SCENARIO = Scenario.model_validate(
    {
        "id": "refine-test",
        "title": "Проверка уточнения",
        "type": "fire",
        "level": "L3",
        "persona": {"base": "calm", "arc": [{"stage": "registration", "mood": "calm"}]},
        "first_line": "У нас крыша горит!",
        "facts": [
            {
                "id": "f_address",
                "value": "улица Станционная, дом 28",
                "refined": "Королёв, улица Станционная, дом 28",
                "reveal_on": {"question": "q_address"},
                "refine_on": "q_city",
            }
        ],
        "checklist": [
            {"id": "q_address", "question": "Назовите адрес", "fact": "f_address"},
            {"id": "q_city", "question": "Это точно Москва город?"},
        ],
    }
)


@pytest.fixture
def slots():
    return SlotMachine(SCENARIO, StemEmbedder(), floor=0.5)


def test_first_answer_is_the_address_the_caller_named(slots):
    turn = slots.hear("Назовите адрес")
    assert turn.revealed == ["f_address"]
    assert slots.value_of("f_address") == "улица Станционная, дом 28"


def test_refinement_replaces_the_value(slots):
    slots.hear("Назовите адрес")
    turn = slots.hear("Это точно Москва город?")
    assert turn.refined == ["f_address"]
    assert slots.value_of("f_address") == "Королёв, улица Станционная, дом 28"


def test_refinement_is_not_a_repeat(slots):
    """Оператор спросил о другом и получил другое — раздражать звонящего
    за правильный вопрос нельзя."""
    slots.hear("Назовите адрес")
    turn = slots.hear("Это точно Москва город?")
    assert turn.repeated == [], "уточнение засчитано повтором"


def test_caller_says_the_refined_value(slots):
    persona = PersonaState(SCENARIO.persona)
    caller = TemplateCaller()
    import asyncio

    asyncio.run(caller.reply(slots.hear("Назовите адрес"), persona, slots))
    line = asyncio.run(caller.reply(slots.hear("Это точно Москва город?"), persona, slots))
    assert "Королёв" in line.text, f"звонящий не поправился: {line.text}"


def test_refining_before_asking_still_reveals(slots):
    """Оператор начал с уточнения — факт всё равно считается раскрытым,
    иначе звонящий промолчит о том, что сам только что назвал."""
    turn = slots.hear("Это точно Москва город?")
    assert "f_address" in turn.revealed
    assert slots.value_of("f_address") == "Королёв, улица Станционная, дом 28"


def test_refinement_happens_once(slots):
    slots.hear("Это точно Москва город?")
    again = slots.hear("Это точно Москва город?")
    assert again.refined == []


# ── сценарий из библиотеки и оценка ──


@pytest.fixture(scope="module")
def library_scenario():
    return load_file(LIBRARY / "fire-private-house-l3.yaml", LIBRARY)


def test_library_scenario_expects_the_refined_address(library_scenario):
    """Эталон — уточнённый адрес: записавший первичный отправит расчёт
    не в тот регион."""
    assert "Королёв" in library_scenario.ground_truth.address
    assert any(fact.refine_on for fact in library_scenario.facts)


def test_common_checklist_item_is_opt_in(library_scenario):
    """Формулировки общего пункта подставились из common.yaml, а в сценарий
    без него он не попал."""
    item = next(i for i in library_scenario.checklist if i.id == "q_address_check")
    assert item.question, "текст общего пункта не подставился"
    assert len(item.examples) >= 5

    other = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    assert "q_address_check" not in {i.id for i in other.checklist}


def _score(scenario, refined: list[str]):
    from app.domain.kio import KIO

    timers = SessionTimers()
    timers.on_event("call.incoming", now=0.0)
    timers.on_event("call.answer", now=5.0)
    return evaluate(
        scenario=scenario,
        kio=KIO(address=scenario.ground_truth.address),
        timers=timers,
        revealed_facts=[fact.id for fact in scenario.facts],
        refined_facts=refined,
    )


def test_not_refining_is_an_e1(library_scenario):
    result = _score(library_scenario, refined=[])
    metric = next(m for m in result.metrics if m.key == "address_refined")
    assert not metric.passed
    assert any(finding.code.value == "E1" for finding in result.findings)


def test_refining_passes(library_scenario):
    result = _score(library_scenario, refined=["f_address"])
    metric = next(m for m in result.metrics if m.key == "address_refined")
    assert metric.passed


def test_scenario_without_refinement_has_no_such_metric():
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    result = _score(scenario, refined=[])
    assert not [m for m in result.metrics if m.key == "address_refined"]


@pytest.mark.skipif(
    not (MODEL / "model_quantized.onnx").exists(), reason="нет модели эмбеддингов — make models"
)
def test_real_model_tells_the_two_questions_apart(library_scenario):
    """Главный риск механики: «назовите адрес» и «это точно Москва?» — оба про
    адрес, и модель могла бы засчитать их одному пункту."""
    from app.dialog.embeddings import E5Embedder

    machine = SlotMachine(library_scenario, E5Embedder(MODEL))
    assert machine.hear("Назовите адрес").matched == ["q_address"]
    assert machine.hear("Это точно Москва?").matched == ["q_address_check"]
