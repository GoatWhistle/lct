"""Справочник ЕКП и карточка, которая из него считается.

Проверяются пункты приёмки lct-32: признаки дают код, список оповещения
производен и не выбирается руками, сценарий размечается признаками.
"""

from pathlib import Path

import pytest
from app.domain import ekp
from app.domain.kio import KIO, derive_incident
from app.scenarios.loader import load_library


def test_reference_loads_whole_book():
    """Импорт не должен незаметно потерять часть строк: их 1283, и это число
    сверяется с книгой заказчика."""
    reference = ekp.reference()
    assert reference.version == "046.24"
    assert len(reference.incidents) == 1283
    assert len(reference.groups) == 23
    assert all(incident.type for incident in reference.incidents), (
        "код без итогового типа"
    )


def test_all_customer_ticket_cards_are_complete_and_classified():
    cards = [
        scenario
        for scenario in load_library(Path(__file__).resolve().parents[2] / "scenarios")
        if scenario.ticket is not None
    ]
    assert len(cards) == 96
    assert {scenario.ticket for scenario in cards} == set(range(1, 33))
    for ticket in range(1, 33):
        assert {
            scenario.position for scenario in cards if scenario.ticket == ticket
        } == {1, 2, 3}
    assert all(scenario.facts and scenario.ground_truth.address for scenario in cards)
    unclassified_ids = {
        "t02-3-car-in-water",  # источник не уточняет, был ли человек в воде
        "t03-2-loud-music",  # время, нужное для признака тишины, не дано
        "t05-3-worker-in-pit",  # падение в котлован не означает обрушение/коммуникации
        "t22-3-suicide-sms",  # намерение в СМС не подтверждает попытку/приготовление
        "t26-3-death-care-home",  # точный код для смерти в центре не подтверждён
        "t32-1-carjacking",  # срок угона для кода не дан
        "t32-3-street-lights",  # время суток для признака не дано
    }
    assert {scenario.id for scenario in cards if not scenario.signs} == unclassified_ids
    assert all(
        scenario.ground_truth.incident_code is None and not scenario.ground_truth.notify
        for scenario in cards
        if scenario.id in unclassified_ids
    )
    assert all(
        scenario.signs
        and scenario.ground_truth.incident_code
        and ekp.incident(scenario.ground_truth.incident_code)
        for scenario in cards
        if scenario.id not in unclassified_ids
    )
    unknown_victim_counts = {
        scenario.id for scenario in cards if scenario.ground_truth.victims is None
    }
    assert unknown_victim_counts == {
        "t02-2-megafon-consultation",
        "t02-3-car-in-water",
        "t03-2-loud-music",
        "t04-1-balcony-fire",
        "t04-3-open-door",
        "t12-3-men-on-bridge-rail",
        "t07-3-lost-elderly",
        "t08-1-mall-smoke",
        "t14-1-grass-fire-azs",
        "t17-1-fire-alarm",
        "t12-1-restaurant-smoke",
        "t30-3-gas-smell-house",
        "t31-3-gas-pipe-whistle",
        "t11-3-lost-in-forest",
        "t16-1-smoke-column",
        "t18-1-unknown-fire",
        "t24-1-parking-quarrel",
        "t25-1-drunk-at-stop",
        "t23-3-lost-child",
        "t27-1-suspicious-car",
        "t28-1-stranger-at-door",
        "t29-1-ticking-box",
        "t29-3-threat-to-blow-up",
        "t30-1-car-theft-yesterday",
        "t31-1-car-theft-witnessed",
        "t32-1-carjacking",
        "t32-3-street-lights",
    }


def test_ticket_two_preserves_moscow_and_does_not_invent_missing_victim_data():
    root = Path(__file__).resolve().parents[2] / "scenarios"
    cards = {
        scenario.id: scenario
        for scenario in load_library(root)
        if scenario.id
        in {
            "t02-1-smoke-chute",
            "t02-2-megafon-consultation",
            "t02-3-car-in-water",
        }
    }
    assert len(cards) == 3
    for scenario in cards.values():
        assert scenario.ground_truth.address.startswith("Москва,")
        address_fact = next(fact for fact in scenario.facts if fact.id == "f_address")
        assert address_fact.value.startswith("Москва,")

    consultation = cards["t02-2-megafon-consultation"]
    victim_fact = next(fact for fact in consultation.facts if fact.id == "f_victims")
    assert victim_fact.value == "в исходном билете сведения о пострадавших не указаны"
    assert consultation.ground_truth.victims is None


def test_ticket_14_refined_address_retains_azs_and_approach_landmarks():
    scenario = next(
        scenario
        for scenario in load_library(Path(__file__).resolve().parents[2] / "scenarios")
        if scenario.id == "t14-1-grass-fire-azs"
    )
    address = next(fact for fact in scenario.facts if fact.id == "f_address")
    for landmark in ("АЗС", "Роснефть", "не доезжая до Расторгуевского шоссе"):
        assert landmark in address.value
        assert landmark in address.refined
        assert landmark in scenario.ground_truth.address
    assert "25 км по столбам в сторону Москвы" in address.refined
    assert "владение 2" in scenario.ground_truth.address


def test_ticket_05_02_does_not_invent_callers_age():
    scenario = next(
        scenario
        for scenario in load_library(Path(__file__).resolve().parents[2] / "scenarios")
        if scenario.id == "t05-2-wrong-medicine"
    )
    who = next(fact for fact in scenario.facts if fact.id == "f_who")
    assert who.value == "Иванова Ирина Петровна, дата рождения 10.03.1975"
    assert "на вид" not in who.value


def test_ticket_25_01_refined_address_keeps_stop_and_side_landmarks():
    scenario = next(
        scenario
        for scenario in load_library(Path(__file__).resolve().parents[2] / "scenarios")
        if scenario.id == "t25-1-drunk-at-stop"
    )
    address = next(fact for fact in scenario.facts if fact.id == "f_address")
    assert "62" in address.refined
    assert "Штурвальная" in address.refined
    assert "на стороне улицы Фабрициуса, дом 18" in address.refined


def test_ticket_address_code_is_not_guessed_to_be_an_intercom():
    root = Path(__file__).resolve().parents[2] / "scenarios"
    expected_codes = {
        "t04-3-open-door": "45В",
        "t05-2-wrong-medicine": "142",
        "t17-1-fire-alarm": "2215",
        "t18-2-husband-wont-wake": "5В",
        "t22-1-flat-fight": "2В",
        "t23-1-drunk-husband": "80В",
        "t28-1-stranger-at-door": "100",
        "t29-3-threat-to-blow-up": "67",
        "t31-3-gas-pipe-whistle": "5В",
    }
    cards = {
        scenario.id: scenario
        for scenario in load_library(root)
        if scenario.id in expected_codes
    }
    assert cards.keys() == expected_codes.keys()
    for scenario_id, code in expected_codes.items():
        address = cards[scenario_id].ground_truth.address
        assert address.endswith(f"код {code}")
        assert "домофон" not in address.casefold()


def test_ticket_09_02_retains_the_source_motorway_designation_in_caller_facts():
    scenario = next(
        scenario
        for scenario in load_library(Path(__file__).resolve().parents[2] / "scenarios")
        if scenario.id == "t09-2-labour"
    )
    address_fact = next(fact for fact in scenario.facts if fact.id == "f_address")
    assert "Горьковского шоссе (М7)" in address_fact.value
    assert "Горьковского шоссе (М7)" in scenario.ground_truth.address


def test_ticket_18_01_retains_both_source_motorway_designations_in_caller_facts():
    scenario = next(
        scenario
        for scenario in load_library(Path(__file__).resolve().parents[2] / "scenarios")
        if scenario.id == "t18-1-unknown-fire"
    )
    address_fact = next(fact for fact in scenario.facts if fact.id == "f_address")
    assert "М3" in address_fact.value
    assert "М1" in address_fact.value
    assert "М3" in scenario.ground_truth.address
    assert "М1" in scenario.ground_truth.address


def test_ticket_8_2_preserves_both_highway_designations_from_source():
    scenario = next(
        item
        for item in load_library(Path(__file__).resolve().parents[2] / "scenarios")
        if item.id == "t08-2-unconscious-roadside"
    )
    address_fact = next(fact for fact in scenario.facts if fact.id == "f_address")
    for designation in ("М3", "М1"):
        assert designation in address_fact.value
        assert designation in scenario.ground_truth.address


def test_ticket_five_preserves_source_observation_without_inventing_age():
    cards = {
        item.id: item
        for item in load_library(Path(__file__).resolve().parents[2] / "scenarios")
        if item.id in {"t05-1-window-fire-people", "t05-2-wrong-medicine"}
    }
    assert len(cards) == 2
    fire_facts = {fact.id: fact.value for fact in cards["t05-1-window-fire-people"].facts}
    assert fire_facts["f_observer"] == "заявитель наблюдает за пожаром с улицы"
    medicine_facts = {fact.id: fact.value for fact in cards["t05-2-wrong-medicine"].facts}
    assert medicine_facts["f_who"] == "Иванова Ирина Петровна, дата рождения 10.03.1975"


def test_ticket_seven_preserves_ambiguous_address_code_verbatim():
    scenario = next(
        item
        for item in load_library(Path(__file__).resolve().parents[2] / "scenarios")
        if item.id == "t07-3-lost-elderly"
    )
    assert scenario.ground_truth.address.endswith("код 5В")
    assert "домофон" not in scenario.ground_truth.address


def test_unknown_ticket_victim_count_is_not_scored():
    from app.scoring.card import evaluate_card

    cards = [
        scenario
        for scenario in load_library(Path(__file__).resolve().parents[2] / "scenarios")
        if scenario.ticket is not None and scenario.ground_truth.victims is None
    ]
    assert len(cards) == 27
    assert all(
        "victims_count"
        not in {metric.key for metric in evaluate_card(scenario, KIO()).metrics}
        for scenario in cards
    )


def test_signs_give_the_code_from_the_book():
    found = ekp.by_signs(["жилой дом", "подъезд", "открытое пламя"])
    assert found is not None
    assert found.code == "1050701"
    assert found.type == "пожар: подъезд"


def test_signs_are_case_insensitive():
    """Методист пишет «Жилой дом», книга — «жилой дом»."""
    assert ekp.by_signs(["Жилой дом", "Подъезд", "Открытое пламя"]) is not None


def test_next_level_narrows_by_previous():
    first = ekp.signs_at_level(1)
    assert "жилой дом" in first
    second = ekp.signs_at_level(2, ["жилой дом"])
    assert "балкон" in second
    assert "мусор" not in second, "«мусор» относится к улице, а не к жилому дому"


def test_category_filters_all_three_incident_choices_and_card_derivation():
    fire = ekp.by_signs(["жилой дом", "балкон", "открытое пламя"])
    assert fire is not None
    other_group = next(group.id for group in ekp.groups() if group.id != fire.group)
    assert ekp.by_signs(fire.signs, fire.group) == fire
    assert ekp.by_signs(fire.signs, other_group) is None
    assert fire.signs[0] in ekp.signs_at_level(1, group=fire.group)
    assert fire.signs[1] in ekp.signs_at_level(2, fire.signs[:1], group=fire.group)
    assert fire.signs[2] in ekp.signs_at_level(3, fire.signs[:2], group=fire.group)
    card = derive_incident(KIO(incident_group=fire.group, signs=fire.signs))
    assert card.incident_code == fire.code
    assert (
        derive_incident(KIO(incident_group=other_group, signs=fire.signs)).incident_code
        is None
    )


def test_service_rows_hidden_from_operator_are_not_offered():
    """Часть строк книги существует ради интеграции и оператору не показывается."""
    assert all(ekp.HIDDEN_SIGN not in sign.lower() for sign in ekp.signs_at_level(1))


def test_notify_list_is_not_a_single_service():
    """Главное расхождение с прежней моделью: происшествие почти всегда
    поднимает больше одной службы (docs/spec/DATASET.md)."""
    services = ekp.notify_list("1050701")
    assert "Служба 101" in services
    assert len(services) > 1


def test_modifier_adds_services_and_never_removes():
    base = ekp.notify_list("1050701")
    with_gas = ekp.notify_list("1050701", ["gas"])
    assert set(base) < set(with_gas), "модификатор только добавляет службы"
    assert "МОСГАЗ" in with_gas


def test_unknown_code_is_empty_not_an_error():
    """Сценарий без признаков остаётся рабочим: занятие не должно падать
    из-за неразмеченной карточки."""
    assert ekp.notify_list("0000000") == []
    assert ekp.incident("0000000") is None


def test_card_derives_code_and_notify_from_signs():
    card = derive_incident(KIO(signs=["жилой дом", "балкон", "открытое пламя"]))
    assert card.incident_code == "1050201"
    assert "Служба 101" in card.notify


def test_operator_cannot_edit_the_notify_list():
    """В боевом АРМ службу можно добавить, но не удалить, и список целиком
    оператор не переписывает — он производен от признаков."""
    from app.domain.kio import apply_patch

    card = derive_incident(KIO(signs=["жилой дом", "балкон", "открытое пламя"]))
    patched = apply_patch(card, {"notify": [], "incident_code": "9999999"})
    assert patched.notify == card.notify
    assert patched.incident_code == card.incident_code


def test_changing_signs_recalculates_the_card():
    from app.domain.kio import apply_patch

    card = derive_incident(KIO(signs=["жилой дом", "балкон", "открытое пламя"]))
    moved = apply_patch(card, {"signs": ["жилой дом", "квартира", "открытое пламя"]})
    assert moved.incident_code != card.incident_code
    assert moved.notify, "после смены признаков список оповещения пересобирается"


@pytest.mark.parametrize("level", [1, 2, 3])
def test_every_level_offers_something(level):
    prefix = ["жилой дом", "подъезд"][: level - 1]
    assert ekp.signs_at_level(level, prefix), f"уровень {level} пуст"


def test_victims_bring_the_ambulance():
    """Модификатор приходит не из классификатора, а из карточки: пострадавшие
    в поле `victims_count` поднимают скорую (docs/spec/DATASET.md)."""
    from app.domain.kio import KIO, derive_incident

    quiet = derive_incident(
        KIO(signs=["Драка", "Улица общественное место", "Массовая (от 10 человек)"])
    )
    hurt = derive_incident(
        KIO(
            signs=["Драка", "Улица общественное место", "Массовая (от 10 человек)"],
            victims_count=5,
        )
    )
    assert "СМП" not in quiet.notify
    assert "СМП" in hurt.notify
    assert set(quiet.notify) < set(hurt.notify), "модификатор только добавляет службы"


def test_gasified_object_brings_mosgaz():
    from app.domain.kio import KIO, FireDetails, derive_incident

    card = derive_incident(
        KIO(
            signs=["жилой дом", "балкон", "открытое пламя"],
            fire=FireDetails(gasified=True),
        )
    )
    assert "МОСГАЗ" in card.notify


def test_removed_modifier_recalculates_notify_without_stale_service():
    from app.domain.kio import KIO, FireDetails, apply_patch, derive_incident

    card = derive_incident(
        KIO(
            signs=["жилой дом", "балкон", "открытое пламя"],
            fire=FireDetails(gasified=True),
        )
    )
    assert "МОСГАЗ" in card.notify
    updated = apply_patch(card, {"fire.gasified": False})
    assert "МОСГАЗ" not in updated.notify
