from app.scoring.address import address_matches


def test_street_abbreviation_matches_but_similarly_named_street_does_not():
    expected = "Дубнинская улица, дом 10"
    assert address_matches(expected, "ул. Дубнинская, д. 10")
    assert not address_matches(expected, "Дубининская улица, дом 10")


def test_street_type_is_part_of_the_operational_address():
    expected = "Москва, улица Ленина, дом 10"
    assert address_matches(expected, "г. Москва, ул. Ленина, д. 10")
    assert not address_matches(expected, "Москва, переулок Ленина, дом 10")
    assert not address_matches(
        expected, "Москва, улица Ленина и переулок Ленина, дом 10"
    )


def test_house_and_apartment_numbers_cannot_be_swapped():
    expected = "дом 10, квартира 20"
    assert address_matches(expected, "д. 10, кв. 20, подъезд 3")
    assert not address_matches(expected, "дом 20, квартира 10")


def test_corpus_and_building_numbers_keep_their_roles():
    expected = "Москва, улица Мира, дом 5, корпус 1, квартира 20"
    assert address_matches(expected, "г. Москва, ул. Мира, д. 5, корп. 1, кв. 20")
    assert not address_matches(expected, "Москва, ул. Мира, дом 1, корп. 5, кв. 20")


def test_empty_or_partial_operational_address_is_not_a_match():
    assert not address_matches("улица Мира, дом 5", "")
    assert not address_matches("улица Мира, дом 5", "улица Мира")
