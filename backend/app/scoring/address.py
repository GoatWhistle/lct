"""Conservative matching for operational addresses.

Names are compared as whole normalized words (never four-letter prefixes), and
numbered address components remain attached to their labels. Extra detail in a
trainee's address is allowed, but a street typo or swapped house/apartment is
not treated as a match. Explicit road types (for example, street vs. lane) are
also operationally significant.
"""

import re


_ALIASES = {
    "ул": "улица", "улица": "улица",
    "д": "дом", "дом": "дом",
    "корп": "корпус", "корпус": "корпус",
    "стр": "строение", "строение": "строение",
    "кв": "квартира", "квартира": "квартира",
    "под": "подъезд", "подъезд": "подъезд",
    "эт": "этаж", "этаж": "этаж",
    "код": "код", "домофон": "код",
    "г": "город", "город": "город",
    "обл": "область", "область": "область",
    "пр": "проспект", "просп": "проспект", "проспект": "проспект",
    "пер": "переулок", "переулок": "переулок",
    "наб": "набережная", "набережная": "набережная",
    "ш": "шоссе", "шоссе": "шоссе",
}
_COMPONENTS = {"дом", "корпус", "строение", "квартира", "подъезд", "этаж", "код"}
_ROAD_TYPES = {"улица", "проспект", "переулок", "набережная", "шоссе"}
_NON_CONTENT = _COMPONENTS | {
    "город", "область",
} | _ROAD_TYPES


def _tokens(value: str | None) -> list[str]:
    if not value:
        return []
    raw = re.findall(r"[a-zа-яё0-9]+", value.casefold().replace("ё", "е"))
    return [_ALIASES.get(token, token) for token in raw if token != "номер"]


def _components(tokens: list[str]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for index, token in enumerate(tokens[:-1]):
        if token in _COMPONENTS:
            result.setdefault(token, set()).add(tokens[index + 1])
    return result


def address_matches(expected: str | None, supplied: str | None) -> bool:
    """Return true only if all expected address words/components are preserved."""
    expected_tokens = _tokens(expected)
    supplied_tokens = _tokens(supplied)
    if not expected_tokens:
        return bool(supplied_tokens)
    if not supplied_tokens:
        return False

    expected_content = {token for token in expected_tokens if token not in _NON_CONTENT}
    supplied_content = {token for token in supplied_tokens if token not in _NON_CONTENT}
    if not expected_content <= supplied_content:
        return False

    expected_components = _components(expected_tokens)
    supplied_components = _components(supplied_tokens)
    expected_road_types = set(expected_tokens) & _ROAD_TYPES
    supplied_road_types = set(supplied_tokens) & _ROAD_TYPES
    # Тип объекта не является декоративным словом: «улица Ленина» и
    # «переулок Ленина» — разные адреса, даже при одинаковых остальных токенах.
    # Если тип явно указан в ответе, он должен совпасть с источником; краткая
    # форма без типа остаётся допустимой, как и прочие проверенные сокращения.
    if (
        expected_road_types
        and supplied_road_types
        and supplied_road_types != expected_road_types
    ):
        return False
    return all(supplied_components.get(kind) == values
               for kind, values in expected_components.items())
