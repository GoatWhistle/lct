"""make models: веса моделей в backend/models/.

Сейчас качает эмбеддинги для слот-автомата. Распознавание (GigaAM) и синтез
(Silero) добавит карточка lct-02 — после замера на демо-машине.
Докачка продолжается с места обрыва: сеть на стенде бывает медленной.
"""

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"

E5 = "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/"
FILES = {
    "e5-small/config.json": E5 + "config.json",
    "e5-small/tokenizer.json": E5 + "tokenizer.json",
    "e5-small/model_quantized.onnx": E5 + "onnx/model_quantized.onnx",
}


def fetch(url: str, target: Path) -> None:
    partial = target.with_suffix(target.suffix + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-"} if offset else {})
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("ab") as out:
        total = offset + int(response.headers.get("Content-Length", 0))
        while chunk := response.read(1 << 20):
            out.write(chunk)
            done = out.tell()
            if total:
                print(f"\r  {target.name}: {done // (1 << 20)} из {total // (1 << 20)} МБ", end="", flush=True)
    partial.rename(target)
    print()


def main() -> None:
    for relative, url in FILES.items():
        target = MODELS / relative
        if target.exists():
            print(f"  {relative}: уже есть")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        fetch(url, target)
    print("эмбеддинги готовы; GigaAM и Silero — карточка tasks/lct-02-latency-baseline.md")


if __name__ == "__main__":
    sys.exit(main())
