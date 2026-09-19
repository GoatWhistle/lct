"""Справочник ЕКП и карточка, которая из него считается.

Проверяются пункты приёмки lct-32: признаки дают код, список оповещения
производен и не выбирается руками, сценарий размечается признаками.
"""

import pytest

from app.domain import ekp
from app.domain.kio import KIO, derive_incident


def test_reference_loads_whole_book():
    """Импорт не должен незаметно потерять часть строк: их 1283, и это число
    сверяется с книгой заказчика."""
    reference = ekp.reference()
    assert reference.version == "046.24"
    assert len(reference.incidents) == 1283
    assert len(reference.groups) == 23
    assert all(incident.type for incident in reference.incidents), "код без итогового типа"


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
