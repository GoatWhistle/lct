"""make latency: замер бюджета задержки голосового контура по этапам.

Запускать **на той машине, которая поедет на занятие** (docs/arch/BACKEND.md).
Цифры пишутся в docs/LATENCY.md руками вместе с решениями — скрипт только меряет.

Что меряется и что нет:
  * STT — GigaAM v3 RNNT и CTC (int8) на живой речи Golos: задержка и ошибка
    распознавания (WER) против эталонной расшифровки;
  * VAD — Silero VAD на одном окне;
  * TTS — Silero v5 на коротких репликах паникующего звонящего;
  * эмбеддинги — multilingual-e5-small на реплике оператора.
  * LLM не меряется: нужен ключ и сеть до провайдера.

Golos — чистая речь со смартфонов, читающих фразы. Паникующий звонящий и оператор
в стрессе дадут ошибку выше: цифра WER отсюда — нижняя граница.
"""

import json
import os
import platform
import statistics
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
sys.path.insert(0, str(ROOT))

WARMUP = 2


def machine() -> str:
    cpu = "?"
    try:
        for line in open("/proc/cpuinfo", encoding="utf-8"):
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        cpu = platform.processor()
    ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    wsl = "WSL2" if "microsoft" in platform.release().lower() else "нативно"
    return f"{cpu}, ядер {os.cpu_count()}, RAM {ram:.0f} ГБ, {platform.system()} {wsl}"


def stats(values_ms: list[float]) -> str:
    ordered = sorted(values_ms)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    digits = 2 if p95 < 10 else 0  # VAD укладывается в доли миллисекунды
    return f"медиана {statistics.median(ordered):.{digits}f} мс, p95 {p95:.{digits}f} мс"


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path)) as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    assert width == 2, f"{path.name}: ожидался PCM16"
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, rate


def normalize(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    return "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text).split()


def wer(reference: list[str], hypothesis: list[str]) -> tuple[int, int]:
    """Расстояние Левенштейна по словам. Возвращает (ошибок, слов в эталоне)."""
    prev = list(range(len(hypothesis) + 1))
    for i, ref_word in enumerate(reference, 1):
        cur = [i] + [0] * len(hypothesis)
        for j, hyp_word in enumerate(hypothesis, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ref_word != hyp_word))
        prev = cur
    return prev[-1], len(reference)


def measure_stt() -> None:
    import onnx_asr

    corpus = MODELS / "samples" / "golos"
    manifest = json.load(open(corpus / "manifest.json", encoding="utf-8-sig"))["items"]
    clips = [(read_wav(corpus / item["file"]), item["text"]) for item in manifest]
    total_audio = sum(len(audio) / rate for (audio, rate), _ in clips)
    print(f"\n## STT — Golos, {len(clips)} фраз, {total_audio:.0f} с речи")

    for name in ("gigaam-v3-rnnt", "gigaam-v3-ctc"):
        started = time.monotonic()
        model = onnx_asr.load_model(name, MODELS / "gigaam-v3-onnx", quantization="int8")
        load_s = time.monotonic() - started

        for (audio, rate), _ in clips[:WARMUP]:
            model.recognize(audio, sample_rate=rate)

        latencies, errors, words, audio_s, compute_s = [], 0, 0, 0.0, 0.0
        worst, durations = [], []
        for (audio, rate), reference in clips:
            started = time.monotonic()
            hypothesis = model.recognize(audio, sample_rate=rate)
            elapsed = time.monotonic() - started
            latencies.append(elapsed * 1000)
            durations.append(len(audio) / rate)
            audio_s += len(audio) / rate
            compute_s += elapsed
            e, n = wer(normalize(reference), normalize(hypothesis))
            errors, words = errors + e, words + n
            if e:
                worst.append(f"    «{reference}» → «{hypothesis}»")

        print(f"- {name} int8: загрузка {load_s:.1f} с; фраза {stats(latencies)}; "
              f"RTF {compute_s / audio_s:.3f}; WER {100 * errors / words:.1f}% ({errors} из {words} слов)")
        # Время растёт с длиной фразы, а у Golos фразы длиннее вопросов оператора:
        # медиана по корпусу завышает задержку. Считаем зависимость от длительности.
        slope, intercept = np.polyfit(durations, latencies, 1)
        span = f"{min(durations):.1f}–{max(durations):.1f} с"
        print(f"  зависимость: ≈ {intercept:.0f} мс + {slope:.0f} мс × секунд речи (фразы корпуса {span})")
        print("  " + "; ".join(f"{sec} с → ~{intercept + slope * sec:.0f} мс" for sec in (1.5, 2, 3)))
        for line in worst[:5]:
            print(line)


def measure_vad() -> None:
    import onnxruntime as ort

    path = MODELS / "silero-vad" / "silero_vad.onnx"
    session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    inputs = {i.name: i for i in session.get_inputs()}
    window = 512  # окно Silero VAD при 16 кГц — 32 мс
    feed = {"input": np.zeros((1, window), dtype=np.float32), "sr": np.array(16000, dtype=np.int64)}
    if "state" in inputs:
        feed["state"] = np.zeros((2, 1, 128), dtype=np.float32)
    else:
        feed["h"] = np.zeros((2, 1, 64), dtype=np.float32)
        feed["c"] = np.zeros((2, 1, 64), dtype=np.float32)
    for _ in range(50):
        session.run(None, feed)
    runs = []
    for _ in range(500):
        started = time.monotonic()
        session.run(None, feed)
        runs.append((time.monotonic() - started) * 1000)
    print(f"\n## VAD — Silero, окно 32 мс\n- на окно: {stats(runs)} (задержка endpointing задана конфигом: 600 мс)")


def measure_tts() -> None:
    path = MODELS / "silero-tts" / "v5_ru.pt"
    if not path.exists():
        print("\n## TTS\n- не измерено: нет models/silero-tts/v5_ru.pt")
        return
    import torch

    torch.set_num_threads(4)
    # Профилирующий компилятор TorchScript на новых длинах входа ничего не выигрывает:
    # без него синтез на 13% быстрее на тех же фразах.
    torch._C._jit_set_profiling_executor(False)
    started = time.monotonic()
    importer = torch.package.PackageImporter(str(path))
    model = importer.load_pickle("tts_models", "model")
    model.to(torch.device("cpu"))
    load_s = time.monotonic() - started
    speakers = getattr(model, "speakers", [])
    speaker = "xenia" if "xenia" in speakers else speakers[0]

    warmup = ["Алло!", "Горит балкон на пятом этаже!", "Скорее приезжайте, пожалуйста, мы задыхаемся!"]
    for text in warmup:
        model.apply_tts(text=text, speaker=speaker, sample_rate=24000)

    # Реплики паникующего звонящего разной длины: время синтеза растёт с длиной звука,
    # и первая фраза звонящего — самая важная для ощущения «ответил сразу».
    lines = [
        "Алло! Помогите!",
        "Горим!",
        "Дым идёт в подъезд!",
        "Жена с ребёнком в дальней комнате!",
        "Я не знаю, где перекрыть газ!",
        "Быстрее, пожалуйста, дышать нечем!",
        "Муж пытался потушить, но не получилось!",
        "Пятый этаж, подъезд второй!",
    ]
    latencies, audio_s, compute_s = [], 0.0, 0.0
    per_line = []
    for text in lines:
        started = time.monotonic()
        audio = model.apply_tts(text=text, speaker=speaker, sample_rate=24000)
        elapsed = time.monotonic() - started
        latencies.append(elapsed * 1000)
        audio_s += len(audio) / 24000
        compute_s += elapsed
        per_line.append(f"«{text}» {len(audio) / 24000:.1f} с звука → {elapsed * 1000:.0f} мс")
    print(f"\n## TTS — Silero v5, голос {speaker}, 24 кГц, 4 потока, без профилирующего компилятора")
    print(f"- загрузка {load_s:.1f} с; реплика {stats(latencies)}; RTF {compute_s / audio_s:.3f} "
          f"(синтез в {audio_s / compute_s:.0f} раз быстрее реального времени)")
    for line in per_line:
        print(f"  {line}")
    print(f"- голоса: {', '.join(speakers)}")


def measure_embeddings() -> None:
    from app.dialog.embeddings import E5Embedder

    embedder = E5Embedder(MODELS / "e5-small")
    embedder.embed(["разогрев"])
    runs = []
    for _ in range(50):
        started = time.monotonic()
        embedder.embed(["На каком этаже пожар?"])
        runs.append((time.monotonic() - started) * 1000)
    print(f"\n## Эмбеддинги — multilingual-e5-small int8\n- реплика: {stats(runs)}")


if __name__ == "__main__":
    print(f"# Замер задержки\n\nМашина: {machine()}")
    only = set(sys.argv[1:])
    for name, step in [("stt", measure_stt), ("vad", measure_vad), ("tts", measure_tts), ("emb", measure_embeddings)]:
        if not only or name in only:
            step()
