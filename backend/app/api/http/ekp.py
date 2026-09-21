"""Справочник ЕКП по HTTP: группы происшествий и значения признаков.

Отдавать его курсанту безопасно, в отличие от сценария: это классификатор,
а не содержимое вызова. Оператор в боевом АРМ видит тот же список и выбирает
из него — скрывать нечего.

Значения признака зависят от уже выбранных: «жилой дом» → «балкон» → «открытое
пламя». Поэтому уровень запрашивается отдельно, а не отдаётся деревом целиком:
дерево на 1283 листа весит полмегабайта, а нужно из него три десятка строк.
"""

from fastapi import APIRouter, Query

from app.domain import ekp

router = APIRouter(prefix="/api/ekp", tags=["ekp"])


@router.get("/groups")
async def groups() -> dict:
    """Группы происшествий — «категории событий» из ТЗ, тема занятия."""
    return {
        "version": ekp.reference().version,
        "groups": [group.model_dump() for group in ekp.groups()],
    }


@router.get("/signs")
async def signs(prefix: list[str] = Query(default=[]), group: int | None = None) -> dict:
    """Значения следующего признака при уже выбранных.

    Пустой `prefix` — значения первого уровня. Возвращается и найденное
    происшествие, если признаков хватило: по нему курсант видит, что именно
    он классифицировал, до того, как отправит карточку.
    """
    level = len(prefix) + 1
    found = ekp.by_signs(prefix, group) if prefix else None
    return {
        "level": level,
        "values": ekp.signs_at_level(level, prefix, group) if level <= 3 else [],
        "incident": (
            {
                "code": found.code,
                "type": found.type,
                "notify": ekp.notify_list(found.code),
            }
            if found
            else None
        ),
    }
