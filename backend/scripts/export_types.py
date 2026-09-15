"""make types: app/domain/events.py → frontend/src/shared/types/generated.ts

Единственный шов между треками генерируется, а не переписывается руками
(docs/arch/CONTRACT.md). Запускается без тяжёлых зависимостей — нужен только
pydantic, поэтому годится и в pre-commit, и в CI.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.domain.events import EventCatalog  # noqa: E402

OUT = ROOT.parent / "frontend" / "src" / "shared" / "types" / "generated.ts"

HEADER = """// Сгенерировано `make types` из backend/app/domain/events.py.
// Руками не править: правка уедет при следующей генерации.
// Контракт: docs/arch/CONTRACT.md
"""


def oneline(text: str) -> str:
    """Docstring в несколько строк — комментарий в TS в одну."""
    return " ".join(text.split())


def pascal(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_"))


def literal(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def ts_type(node: Any) -> str:
    """JSON Schema → выражение типа TypeScript."""
    if node is True or node is None:
        return "unknown"
    if node is False:
        return "never"
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    if "allOf" in node and len(node["allOf"]) == 1:
        return ts_type(node["allOf"][0])
    if "anyOf" in node:
        return " | ".join(dict.fromkeys(ts_type(item) for item in node["anyOf"]))
    if "oneOf" in node:
        return " | ".join(dict.fromkeys(ts_type(item) for item in node["oneOf"]))
    if "const" in node:
        return literal(node["const"])
    if "enum" in node:
        return " | ".join(literal(value) for value in node["enum"])

    kind = node.get("type")
    if kind == "array":
        if "prefixItems" in node:
            return "[" + ", ".join(ts_type(item) for item in node["prefixItems"]) + "]"
        return f"Array<{ts_type(node.get('items'))}>"
    if kind == "object":
        extra = node.get("additionalProperties")
        if "properties" in node:
            return inline_object(node)
        return f"Record<string, {ts_type(extra)}>"
    if kind == "string":
        return "string"
    if kind in ("integer", "number"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if isinstance(kind, list):
        return " | ".join(ts_type({"type": item}) for item in kind)
    return "unknown"


def fields(node: dict[str, Any], indent: str = "  ") -> list[str]:
    required = set(node.get("required", []))
    lines: list[str] = []
    for name, prop in node.get("properties", {}).items():
        # Дискриминатор всегда присутствует в сериализованном событии,
        # иначе TypeScript не сузит союз по полю `type`.
        optional = "" if name in required or "const" in prop else "?"
        description = prop.get("description")
        if description:
            lines.append(f"{indent}/** {oneline(description)} */")
        lines.append(f"{indent}{name}{optional}: {ts_type(prop)};")
    return lines


def inline_object(node: dict[str, Any]) -> str:
    return "{ " + " ".join(line.strip() for line in fields(node, indent="")) + " }"


def render_def(name: str, node: dict[str, Any]) -> str:
    doc = oneline(node.get("description", ""))
    comment = f"/** {doc} */\n" if doc else ""
    if "enum" in node and "properties" not in node:
        values = " | ".join(literal(value) for value in node["enum"])
        return f"{comment}export type {name} = {values};"
    if "properties" not in node:
        return f"{comment}export type {name} = {ts_type(node)};"
    body = "\n".join(fields(node))
    return f"{comment}export interface {name} {{\n{body}\n}}"


def event_names(defs: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for node in defs.values():
        prop = node.get("properties", {}).get("type", {})
        value = prop.get("const")
        if value is None and isinstance(prop.get("enum"), list) and len(prop["enum"]) == 1:
            value = prop["enum"][0]
        if isinstance(value, str) and value not in names:
            names.append(value)
    return sorted(names)


def render() -> str:
    """Готовый текст generated.ts. Вынесено отдельно, чтобы тест мог
    сверить файл в репозитории с моделями и поймать забытый make types."""
    schema = EventCatalog.model_json_schema(ref_template="#/$defs/{model}")
    defs: dict[str, Any] = schema.get("$defs", {})

    chunks = [HEADER]
    for name in sorted(defs):
        chunks.append(render_def(name, defs[name]))

    chunks.append("// Союзы по каналам — смотри docs/arch/CONTRACT.md#каналы")
    for key, prop in schema.get("properties", {}).items():
        alias, expr = pascal(key), ts_type(prop)
        if alias == expr:
            continue  # союз из одного типа: интерфейс уже объявлен выше
        chunks.append(f"export type {alias} = {expr};")

    names = event_names(defs)
    chunks.append(
        "export type EventName =\n  " + "\n  | ".join(literal(name) for name in names) + ";"
    )

    return "\n\n".join(chunks) + "\n"


def main() -> None:
    text = render()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"{OUT.relative_to(ROOT.parent)}: {text.count('export ')} объявлений")


if __name__ == "__main__":
    main()
