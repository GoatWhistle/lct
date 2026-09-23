"""Проверка локального TTS → Whisper small без микрофона и внешней сети.

Это не замер сквозной задержки звонка: TTS создаёт синтетический образец, а
загрузка моделей оплачивается отдельно. На Windows выполнить ту же команду.
"""

import time

import numpy as np
from scipy.signal import resample_poly

from app.voice.models import Synthesizer, WhisperRecognizer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PHRASE = "Пожар на улице Ленина, дом четырнадцать. Нужна помощь."


def main() -> None:
    started = time.monotonic()
    tts = Synthesizer(ROOT / "models" / "silero-tts" / "v5_ru.pt")
    print(f"TTS загружен за {time.monotonic() - started:.2f} с", flush=True)
    started = time.monotonic()
    pcm = tts.synthesize(PHRASE)
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    audio = resample_poly(audio, 2, 3).astype(np.float32)
    print(f"TTS: {time.monotonic() - started:.2f} с, звук {len(audio) / 16000:.2f} с", flush=True)

    started = time.monotonic()
    stt = WhisperRecognizer("http://127.0.0.1:18082")
    stt.warmup()
    print(f"Whisper server готов за {time.monotonic() - started:.2f} с", flush=True)
    started = time.monotonic()
    answer = stt.transcribe(audio)
    print(f"Whisper: {time.monotonic() - started:.2f} с", flush=True)
    print(f"ожидалось: {PHRASE}\nполучено:  {answer}", flush=True)


if __name__ == "__main__":
    main()
