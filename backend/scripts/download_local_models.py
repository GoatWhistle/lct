"""Скачать ровно проверенные файлы трёх локальных моделей.

Файлы лежат в backend/models/ (не в Git). Скрипт одинаково работает на
Windows, macOS и Linux; при повторном запуске продолжает неполный файл.
После переноса каталога models/ интернет для занятия не нужен.
"""

import hashlib
import argparse
import shutil
import ssl
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"


@dataclass(frozen=True)
class Artifact:
    repo: str
    name: str
    target: str
    size: int
    sha256: str


ARTIFACTS = (
    Artifact("Qwen/Qwen3-1.7B-GGUF", "Qwen3-1.7B-Q8_0.gguf",
             "qwen3-1.7b/Qwen3-1.7B-Q8_0.gguf", 1834426016,
             "061b54daade076b5d3362dac252678d17da8c68f07560be70818cace6590cb1a"),
    Artifact("Vikhrmodels/Vikhr-Llama-3.2-1B-instruct-GGUF",
             "Vikhr-Llama-3.2-1B-Q4_K_M.gguf",
             "vikhr-1b/Vikhr-Llama-3.2-1B-Q4_K_M.gguf", 807694432,
             "a85c0ef4b6476ff5fcf2c961226234ab7008d17087c2152416e61a9b7e74b32d"),
    Artifact("ggerganov/whisper.cpp", "ggml-small-q5_1.bin",
             "whisper-small/ggml-small-q5_1.bin", 190085487,
             "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb"),
)


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            sha.update(chunk)
    return sha.hexdigest()


def verify(item: Artifact) -> None:
    """Проверить перенесённый вес без сети и без изменения файла."""
    target = MODELS / item.target
    if not target.is_file():
        raise RuntimeError(f"нет локального файла: {target}")
    if target.stat().st_size != item.size:
        raise RuntimeError(
            f"неверный размер {item.target}: {target.stat().st_size}, ожидался {item.size} байт"
        )
    actual = digest(target)
    if actual != item.sha256:
        raise RuntimeError(f"SHA-256 не совпал для {item.target}: {actual}")
    print(f"проверено SHA-256: {item.target}", flush=True)


def download(item: Artifact) -> None:
    target = MODELS / item.target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            verify(item)
        except RuntimeError:
            pass
        else:
            print(f"готово: {item.target}", flush=True)
            return
    partial = target.with_name(target.name + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > item.size:
        raise RuntimeError(f"некорректный частичный файл: {partial}")
    free = shutil.disk_usage(MODELS if MODELS.exists() else ROOT).free
    if free < item.size - offset + 256 * 1024 * 1024:
        raise RuntimeError(f"мало свободного места для {item.target}: осталось {free // (1 << 20)} МиБ")
    url = f"https://huggingface.co/{item.repo}/resolve/main/{item.name}"
    request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
    print(f"скачиваю {item.target} ({item.size // (1 << 20)} МиБ)", flush=True)
    with urllib.request.urlopen(
        request, timeout=120, context=ssl.create_default_context(cafile=certifi.where())
    ) as response:
        # Сервер может проигнорировать Range: тогда начинаем заново, не
        # приписываем полный файл к частичному.
        resumed = offset > 0 and response.status == 206
        with partial.open("ab" if resumed else "wb") as out:
            count = offset if resumed else 0
            while chunk := response.read(1 << 20):
                out.write(chunk)
                count += len(chunk)
                if count % (64 << 20) < len(chunk) or count == item.size:
                    print(f"  {count // (1 << 20)}/{item.size // (1 << 20)} МиБ", flush=True)
    if partial.stat().st_size != item.size or digest(partial) != item.sha256:
        raise RuntimeError(f"контроль размера/SHA-256 не пройден: {partial}")
    partial.replace(target)
    print(f"проверено SHA-256: {item.target}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only", action="store_true",
        help="проверить уже загруженные/перенесённые веса без сети и без изменения файлов",
    )
    args = parser.parse_args()
    try:
        for item in ARTIFACTS:
            verify(item) if args.verify_only else download(item)
    except (OSError, RuntimeError) as exc:
        action = "проверка прервана" if args.verify_only else "загрузка прервана"
        print(f"{action}: {exc}", file=sys.stderr)
        return 1
    if args.verify_only:
        print("Все три локальные модели прошли офлайн-проверку; файлы не изменялись.")
    else:
        print("Все три локальные модели проверены. Для голоса нужны также whisper-server, VAD и TTS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
