"""Загрузчик библиотеки. Сломанный сценарий обязан падать на старте
с сообщением, понятным методисту, а не трассировкой pydantic.
"""

from pathlib import Path

import pytest

from app.scenarios.loader import ScenarioError, load_file, load_library

LIBRARY = Path(__file__).resolve().parents[2] / "scenarios"

VALID = """
id: t-1
title: "Проверочный"
type: fire
level: L1
persona: { base: calm }
first_line: "Алло"
facts:
  - { id: f_addr, value: "Ленина, 1", reveal_on: { question: q_addr } }
checklist:
  - { id: q_addr, question: "Адрес?", fact: f_addr }
ground_truth:
  address: "Ленина, 1"
"""


def write(tmp_path: Path, body: str, name: str = "t-1.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def test_library_loads():
    scenarios = load_library(LIBRARY)
    assert scenarios, "библиотека пуста"
    assert all(s.ground_truth.dds for s in scenarios), "ДДС не выведен"


def test_extends_inherits_whole_checklist():
    """Общий чек-лист по классификатору наследуется целиком,
    локальные пункты дополняют его."""
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    ids = [item.id for item in scenario.checklist]
    assert "q_caller" in ids, "пункт из общего чек-листа потерялся"
    assert all(item.question for item in scenario.checklist), "пункт без текста вопроса"


def test_ground_truth_is_derived_not_written():
    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    assert scenario.ground_truth.incident_type.value == "fire"
    assert scenario.ground_truth.dds.value == "01"
    assert scenario.ground_truth.required_facts, "обязательные факты не собраны"


def test_derived_fields_in_yaml_are_rejected(tmp_path):
    """Иначе генератор сценариев разведёт факты и эталон, и курсанта
    оштрафуют за правильный ответ."""
    path = write(tmp_path, VALID + '  dds: "03"\n')
    with pytest.raises(ScenarioError, match="выводится кодом"):
        load_file(path, tmp_path)


def test_broken_yaml_names_the_file(tmp_path):
    path = write(tmp_path, "id: [не закрыт\n")
    with pytest.raises(ScenarioError, match="битый YAML"):
        load_file(path, tmp_path)


def test_hidden_fact_without_approach_is_rejected(tmp_path):
    body = VALID.replace(
        '  - { id: f_addr, value: "Ленина, 1", reveal_on: { question: q_addr } }',
        '  - { id: f_addr, value: "Ленина, 1", hidden: true, reveal_on: { question: q_addr } }',
    )
    with pytest.raises(ScenarioError, match="hidden требует"):
        load_file(write(tmp_path, body), tmp_path)


def test_checklist_pointing_at_missing_fact_is_rejected(tmp_path):
    body = VALID.replace("fact: f_addr }", "fact: f_нет }")
    with pytest.raises(ScenarioError, match="которого нет"):
        load_file(write(tmp_path, body), tmp_path)


def test_typo_in_field_name_is_rejected(tmp_path):
    """Схема строгая: опечатка должна падать на старте, а не игнорироваться."""
    body = VALID.replace("level: L1", "level: L1\nfirst_lines: 'опечатка'")
    with pytest.raises(ScenarioError):
        load_file(write(tmp_path, body), tmp_path)


def test_era_block_requires_era_type(tmp_path):
    body = VALID + """
era_glonass:
  vin: "X"
  coords: { lat: 1, lon: 2 }
  passengers: 1
  impact_force: "сильный"
"""
    with pytest.raises(ScenarioError, match="era_glonass"):
        load_file(write(tmp_path, body), tmp_path)
