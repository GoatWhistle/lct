#!/usr/bin/env python3
"""Export installed Python and Node package licenses to a reviewable manifest."""

from __future__ import annotations

import importlib.metadata as metadata
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "delivery" / "THIRD-PARTY.md"


def python_license(dist: metadata.Distribution) -> str:
    expression = dist.metadata.get("License-Expression")
    if expression:
        return expression.strip()
    value = (dist.metadata.get("License") or "").strip()
    if value and len(value) <= 80 and "\n" not in value:
        return value
    classifiers = dist.metadata.get_all("Classifier") or []
    licenses = [item.rsplit("::", 1)[-1].strip() for item in classifiers if item.startswith("License ::")]
    return ", ".join(licenses) if licenses else "не указана в metadata"


def python_packages() -> list[tuple[str, str, str]]:
    rows = {
        (
            dist.metadata.get("Name") or dist.name,
            dist.version,
            python_license(dist),
        )
        for dist in metadata.distributions()
        if (dist.metadata.get("Name") or dist.name).lower() != "lct-backend"
    }
    return sorted(rows, key=lambda row: row[0].lower())


def node_packages() -> list[tuple[str, str, str]]:
    modules = ROOT / "frontend" / "node_modules"
    rows: set[tuple[str, str, str]] = set()
    if not modules.exists():
        return []
    for package_file in modules.glob("**/package.json"):
        if ".bin" in package_file.parts:
            continue
        try:
            package = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        license_value = package.get("license", "не указана в package.json")
        if isinstance(license_value, dict):
            license_value = license_value.get("type", "не указана в package.json")
        if isinstance(license_value, list):
            license_value = ", ".join(
                str(item.get("type", item)) if isinstance(item, dict) else str(item)
                for item in license_value
            )
        rows.add((name, version, str(license_value)))
    return sorted(rows, key=lambda row: (row[0].lower(), row[1]))


def table(rows: list[tuple[str, str, str]]) -> str:
    def escaped(value: str) -> str:
        return value.replace("|", "\\|")

    body = ["| Пакет | Версия | Лицензия из metadata |", "|---|---:|---|"]
    body.extend(f"| {escaped(name)} | {version} | {escaped(license_name)} |"
                for name, version, license_name in rows)
    return "\n".join(body)


def main() -> None:
    python_rows = python_packages()
    node_rows = node_packages()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    text = f"""# Реестр сторонних пакетов

Дата формирования: 23.09.2026.

Файл сформирован из metadata фактически установленного `backend/.venv` и
`frontend/node_modules`. Он включает прямые и транзитивные зависимости,
доступные в текущем окружении разработки. Авторитетные версии для повторной
установки находятся в `backend/uv.lock` и `frontend/package-lock.json`.
Название лицензии не заменяет её полный текст; при поставке сохраняются файлы
LICENSE и NOTICE каждого пакета, контейнера, исполняемого файла и модели.

## Python пакеты

Пакетов: {len(python_rows)}.

{table(python_rows)}

## Node пакеты

Уникальных сочетаний имени и версии: {len(node_rows)}.

{table(node_rows)}

## Компоненты вне менеджеров пакетов

| Компонент | Версия или источник | Лицензирование |
|---|---|---|
| PostgreSQL | `postgres:16-alpine` | PostgreSQL License; сохранить уведомления образа |
| Python | `python:3.11-slim` | PSF License; сохранить уведомления образа |
| Node.js | `node:20-slim` | MIT и лицензии включённых компонентов |
| uv | образ `ghcr.io/astral-sh/uv:latest` | Apache-2.0 или MIT по репозиторию uv |
| llama.cpp `b10934` | локальный исполняемый файл; собирается в образе `lct-hack-llm` | MIT; не входит в Git проекта |
| whisper.cpp | локальный исполняемый файл | MIT; не входит в Git проекта |
| Qwen3 1.7B GGUF Q8_0 | в образе `lct-hack-llm` | Apache 2.0; текст в `deploy/licenses/Qwen3-LICENSE.txt` и `/licenses` образа |
| Vikhr-Llama-3.2-1B-Instruct GGUF Q4_K_M | в образе `lct-hack-llm` | Llama 3.2 Community License, «Built with Llama»; `deploy/licenses/Llama-3.2-LICENSE.txt`, `Llama-3.2-USE-POLICY.md` |
| GigaAM v3 RNNT ONNX int8 | в образе `lct-hack-backend` | MIT; `deploy/licenses/GigaAM-LICENSE.txt` |
| Silero TTS v5 ru | в образе `lct-hack-backend` | CC BY-NC-SA 4.0, некоммерческое использование; `deploy/licenses/Silero-TTS-LICENSE.txt` |
| Silero VAD | в образе `lct-hack-backend` | MIT; `deploy/licenses/Silero-VAD-LICENSE.txt` |
| multilingual-e5-small ONNX | в образе `lct-hack-backend` | MIT по карточке модели `intfloat/multilingual-e5-small` |
| Whisper small GGML | отдельная загрузка из whisper.cpp, в образы не входит | сохранить LICENSE и model card конкретной версии |
| Asterisk | пакет Ubuntu 22.04 в образе `lct-hack-sip` | GPL-2.0; сохранить уведомления пакета |
"""
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"{OUTPUT}: python={len(python_rows)}, node={len(node_rows)}")


if __name__ == "__main__":
    main()
