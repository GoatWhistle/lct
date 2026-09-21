"""make models: веса моделей в backend/models/.

Эмбеддинги, распознавание (GigaAM v3, int8) и VAD качаются с Hugging Face.
Синтез (Silero TTS v5) лежит на models.silero.ai — российском хосте, который
не отвечает из-под VPN: его скрипт не качает, а проверяет и говорит, что делать.
Докачка продолжается с места обрыва: сеть на стенде бывает медленной.
"""

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"

E5 = "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/"
GIGAAM = "https://huggingface.co/istupakov/gigaam-v3-onnx/resolve/main/"
VAD = "https://huggingface.co/istupakov/silero-vad-onnx/resolve/main/"
FILES = {
    "e5-small/config.json": E5 + "config.json",
    "e5-small/tokenizer.json": E5 + "tokenizer.json",
    "e5-small/model_quantized.onnx": E5 + "onnx/model_quantized.onnx",
    # RNNT — основная модель, CTC — запасная на случай, если RNNT не загрузится
    # (по скорости CTC не выигрывает — docs/LATENCY.md).
    "gigaam-v3-onnx/config.json": GIGAAM + "config.json",
    "gigaam-v3-onnx/v3_vocab.txt": GIGAAM + "v3_vocab.txt",
    "gigaam-v3-onnx/v3_rnnt_encoder.int8.onnx": GIGAAM + "v3_rnnt_encoder.int8.onnx",
    "gigaam-v3-onnx/v3_rnnt_decoder.int8.onnx": GIGAAM + "v3_rnnt_decoder.int8.onnx",
    "gigaam-v3-onnx/v3_rnnt_joint.int8.onnx": GIGAAM + "v3_rnnt_joint.int8.onnx",
    "gigaam-v3-onnx/v3_ctc.int8.onnx": GIGAAM + "v3_ctc.int8.onnx",
    "silero-vad/config.json": VAD + "config.json",
    "silero-vad/silero_vad.onnx": VAD + "silero_vad.onnx",
}

SILERO_TTS = "silero-tts/v5_ru.pt"
SILERO_TTS_URL = "https://models.silero.ai/models/tts/ru/v5_ru.pt"


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
    tts = MODELS / SILERO_TTS
    if tts.exists():
        print(f"  {SILERO_TTS}: уже есть")
        print("все модели на месте")
        return 0
    # Не качаем с зеркал: .pt грузится через pickle, файл из непроверенного
    # источника — это чужой код на стенде.
    print(f"""
  {SILERO_TTS}: НЕТ. Хост models.silero.ai не отвечает из-под VPN — скачайте вручную:
    {SILERO_TTS_URL}
  и положите в backend/models/{SILERO_TTS}""")
    return 1


if __name__ == "__main__":
    sys.exit(main())
