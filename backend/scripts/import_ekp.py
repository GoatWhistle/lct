"""make ekp: классификатор происшествий заказчика → нормализованный JSON.

Исходник — рабочая книга Excel на 1283 строки и 104 колонки
(docs/spec/source/, разбор в docs/spec/DATASET.md). Читать её в рантайме нельзя:
это лишняя зависимость (openpyxl) и полсекунды на старте ради данных,
которые не меняются между занятиями. Поэтому импорт разовый, результат —
`app/domain/ekp.json` под гитом.

Структура книги: три строки заголовков.
  строка 0 — группы колонок («Классификатор МЧС» накрывает и 101, и ОДС ПСЦ);
  строка 1 — имена колонок данных, а для части служб — их названия;
  строка 2 — модификаторы («выбран признак НД - НЕТ ДОСТУПА»).
Имя службы и модификатор лежат то в первой строке, то во второй, то склеены
в одну ячейку видом «ОДС ПСЦ (выбран признак УЛ)» — отсюда разбор ниже.

    make ekp
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / "docs" / "spec" / "source" / "Классификатор-происшествий-v046.24.xlsx"
OUTPUT = ROOT / "app" / "domain" / "ekp.json"
VERSION = "046.24"

#: Колонки данных до начала служб.
COL_GROUP, COL_CODE, COL_TITLE = 0, 4, 5
COL_SIGN1, COL_SIGN2, COL_SIGN3, COL_EXTRA = 6, 7, 8, 9
COL_TYPE, COL_EKP35, COL_MAIN = 10, 11, 12
COL_FIRST_SERVICE = 13

#: Модификаторы, которые встречаются у разных служб под разными словами.
#: Приводим к общему ключу — по ним фильтруется список оповещения.
MODIFIER_KEYS: list[tuple[str, str]] = [
    ("нд", "no_access"),
    ("нет доступа", "no_access"),
    ("ул", "life_threat"),
    ("угроза людям", "life_threat"),
    ("пп", "casualties"),
    ("пострадавшие", "casualties"),
    ("погибшие", "casualties"),
    ("постр", "casualties"),
    ("газификац", "gas"),
    ("эвакуац", "evacuation"),
    ("мед. помощь", "medical"),
    ("правонарушение", "offence"),
    ("перекрытие движение", "road_block"),
    ("стройка", "construction"),
    ("тоннель", "tunnel"),
    ("пеш", "pedestrian"),
    ("ав", "vehicle"),
    (">5 чел", "mass"),
    ("на объектах связи", "comm_object"),
    ("объект из перечня", "listed_object"),
]

#: «Модификатор не выбран» — это базовый список оповещения, а не условие.
BASE_MARKERS = ("не выбран", "не выбраны", "признак не выбран", "реагирование всегда")

#: Подколонки одной службы: не условие оповещения, а маршрут доставки карточки
#: («интеграция» против «АРМ-112»). Служба оповещается всегда, поэтому базовый
#: список, а не условный. Разбирались глазами по заголовкам книги — их восемь.
SUBCHANNELS = frozenset(
    {
        "арм-112",
        "интеграция",
        "дежурная служба арм-112",
        "(куб)",
        "куб",
        "пожары",
        "события по полигонам",
        "москва",
        "старый крим",
    }
)

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


def slug(text: str) -> str:
    out = "".join(TRANSLIT.get(ch, ch) for ch in text.lower())
    out = re.sub(r"[^a-z0-9]+", "-", out).strip("-")
    return out or "service"


def clean(value) -> str:
    """Ячейка Excel → строка без переносов и двойных пробелов."""
    return " ".join(str(value).split()) if value is not None else ""


def modifier_key(label: str) -> str | None:
    """Ключ модификатора или None, если это базовый список оповещения."""
    low = label.lower()
    if not low or low in SUBCHANNELS or any(marker in low for marker in BASE_MARKERS):
        return None
    for needle, key in MODIFIER_KEYS:
        if needle in low:
            return key
    # Модификатор есть, но в наш словарь не попал: оставляем как есть, чтобы
    # он был виден в данных, а не потерялся молча.
    return "other:" + slug(label)[:40]


#: «Классификатор МЧС» в верхней строке — заголовок группы колонок, а не служба:
#: под ним живут и «Служба 101», и «ОДС ПСЦ», и «МГПСС».
GROUP_PREFIX = re.compile(r"^Классификатор\s+")

#: Ячейка описывает не службу, а условие оповещения.
MODIFIER_PHRASE = re.compile(r"признак|выбран", re.IGNORECASE)


def split_parens(label: str) -> tuple[str, str] | None:
    """«ОДС ПСЦ (выбран признак УЛ)» → («ОДС ПСЦ», «выбран признак УЛ»)."""
    match = re.match(r"^(.*?)\s*\((.*)\)\s*$", label)
    if match and match.group(1) and match.group(2):
        return GROUP_PREFIX.sub("", match.group(1).strip()), match.group(2).strip()
    return None


def build_channels(rows) -> list[dict]:
    """Колонки служб: имя службы, модификатор, ключ модификатора.

    Имя службы лежит в разных строках в зависимости от колонки, поэтому порядок
    проверок важен. `owner` — последняя служба из верхней строки; её подколонки
    (у ГУП МСР это «КУБ» и «пожары») собственного имени не имеют.
    """
    top, names, mods = rows[0], rows[1], rows[2]
    width = max(len(top), len(names), len(mods))
    channels: list[dict] = []
    group = ""       # текущая «Классификатор X»
    owner = ""       # текущая служба из верхней строки
    owner_is_group = False

    for col in range(COL_FIRST_SERVICE, width):
        head = clean(top[col]) if col < len(top) else ""
        if head:
            owner_is_group = bool(GROUP_PREFIX.match(head))
            if owner_is_group:
                group = GROUP_PREFIX.sub("", head)
                owner = group
            else:
                owner = head
        name_cell = clean(names[col]) if col < len(names) else ""
        mod_cell = clean(mods[col]) if col < len(mods) else ""

        if head and not owner_is_group:
            # Своя служба в верхней строке: всё остальное — условие.
            name, modifier = head, name_cell or mod_cell
        elif (parts := split_parens(name_cell) or split_parens(mod_cell)) is not None:
            name, modifier = parts
        elif name_cell and not MODIFIER_PHRASE.search(name_cell) and owner_is_group:
            # «Служба 101», «ОДС ПСЦ», «МГПСС» — имя службы внутри группы.
            name, modifier = name_cell, mod_cell
        elif name_cell and not MODIFIER_PHRASE.search(name_cell):
            # Подколонка чужой службы: «КУБ», «пожары», «интеграция».
            name, modifier = owner, name_cell
        else:
            name, modifier = owner, name_cell or mod_cell

        channels.append(
            {
                "col": col,
                "name": name or f"колонка {col}",
                "modifier": modifier,
                "key": modifier_key(modifier),
            }
        )
    return channels


def main() -> int:
    try:
        import openpyxl
    except ImportError:
        print("нужен openpyxl: uv run --with openpyxl python scripts/import_ekp.py")
        return 1

    if not SOURCE.exists():
        print(f"нет файла классификатора: {SOURCE}")
        return 1

    rows = list(
        openpyxl.load_workbook(SOURCE, read_only=True, data_only=True)["Лист1"].iter_rows(
            values_only=True
        )
    )
    channels = build_channels(rows)

    # Службы — без учёта модификатора: «Служба 101» одна, колонок у неё две.
    services: list[dict] = []
    index_by_name: dict[str, int] = {}
    for channel in channels:
        if channel["name"] not in index_by_name:
            index_by_name[channel["name"]] = len(services)
            services.append({"id": slug(channel["name"]), "name": channel["name"]})
        channel["service"] = index_by_name[channel["name"]]

    groups: dict[int, str] = {}
    incidents: list[dict] = []
    group_id = 0

    for row in rows[3:]:
        code = clean(row[COL_CODE] if COL_CODE < len(row) else None)
        if not code.isdigit():
            continue
        if len(code) <= 2:  # строка-заголовок группы: «1 | Пожары и задымления»
            group_id = int(code)
            groups[group_id] = clean(row[COL_TITLE])
            continue

        base: list[int] = []
        conditional: dict[str, list[int]] = {}
        for channel in channels:
            col = channel["col"]
            if col >= len(row) or not row[col]:
                continue
            service = channel["service"]
            if channel["key"] is None:
                if service not in base:
                    base.append(service)
            else:
                bucket = conditional.setdefault(channel["key"], [])
                if service not in bucket:
                    bucket.append(service)

        # Служба, попавшая и в базовый список, и в условный, оповещается всегда.
        for key, bucket in list(conditional.items()):
            rest = [s for s in bucket if s not in base]
            if rest:
                conditional[key] = rest
            else:
                del conditional[key]

        signs = [clean(row[c]) for c in (COL_SIGN1, COL_SIGN2, COL_SIGN3)]
        incidents.append(
            {
                "code": code,
                "group": int(clean(row[COL_GROUP]) or group_id or 0),
                "signs": [s for s in signs if s],
                "extra": clean(row[COL_EXTRA]) or None,
                "type": clean(row[COL_TYPE]),
                "ekp": clean(row[COL_EKP35]),
                "main": clean(row[COL_MAIN]) or None,
                "notify": base,
                "notify_if": conditional,
            }
        )

    payload = {
        "version": VERSION,
        "source": SOURCE.name,
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "groups": [{"id": gid, "title": title} for gid, title in sorted(groups.items())],
        "services": services,
        "incidents": incidents,
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )

    unknown = sorted({
        key for incident in incidents for key in incident["notify_if"] if key.startswith("other:")
    })
    if unknown:
        print("  модификаторы без разбора (смотреть глазами): " + ", ".join(unknown))

    no_type = sum(1 for i in incidents if not i["type"])
    print(f"{OUTPUT.relative_to(ROOT.parent)}: {len(incidents)} кодов, {len(services)} служб, "
          f"{len(groups)} групп, {OUTPUT.stat().st_size // 1024} КБ")
    if no_type:
        print(f"  без итогового типа: {no_type} — проверить руками")
    return 0


if __name__ == "__main__":
    sys.exit(main())
