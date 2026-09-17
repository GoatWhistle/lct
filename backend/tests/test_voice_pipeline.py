"""Голосовой контур на настоящих моделях: речь оператора → голос звонящего.

Речь оператора синтезируется мужским голосом Silero и подаётся кадрами по 20 мс
в реальном времени — как с микрофона. Синтетическая речь распознаётся легче живой:
задержка отсюда честная, а качество распознавания — оптимистичное.

Без моделей тест пропускается: make models.
"""

import asyncio
import importlib.util
import re
import time
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from app.domain.events import CallerUtterance, SttFinal, TtsBegin, TtsCancel, TtsEnd

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
LIBRARY = ROOT.parent / "scenarios"

# Пропуск, а не падение: и без файлов моделей, и без библиотек голоса (torch,
# onnx-asr) — их нет в `make test` и в контейнере, это отдельная группа зависимостей.
pytestmark = pytest.mark.skipif(
    not all(importlib.util.find_spec(name) for name in ("torch", "onnx_asr"))
    or not all(
        (MODELS / path).exists()
        for path in ("gigaam-v3-onnx/v3_rnnt_encoder.int8.onnx", "silero-vad/silero_vad.onnx",
                     "silero-tts/v5_ru.pt", "e5-small/model_quantized.onnx")
    ),
    reason="голос не установлен: make models и uv sync --extra voice, запуск — make test-voice",
)


@pytest.fixture(scope="module")
def models():
    from app.voice.models import Recognizer, Synthesizer, VoiceModels

    recognizer = Recognizer(MODELS / "gigaam-v3-onnx")
    synthesizer = Synthesizer(MODELS / "silero-tts" / "v5_ru.pt")
    recognizer.warmup()
    synthesizer.warmup()
    return VoiceModels(recognizer, synthesizer, MODELS / "silero-vad" / "silero_vad.onnx")


@pytest.fixture(scope="module")
def embedder():
    from app.dialog.embeddings import E5Embedder

    return E5Embedder(MODELS / "e5-small")


def operator_speech(models, text: str) -> bytes:
    """Реплика оператора мужским голосом, 16 кГц PCM16 — формат микрофона."""
    spoken = models.synthesizer._model.apply_tts(text=text, speaker="aidar", sample_rate=24_000).numpy()
    target = np.arange(0, len(spoken), 24_000 / 16_000)
    audio = np.interp(target, np.arange(len(spoken)), spoken)
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()


def last_voiced_byte(pcm: bytes) -> int:
    """Конец речи по звуку, а не по последнему кадру: у синтезированной фразы
    в конце своя тишина, и отсчёт от последнего кадра занижал бы задержку."""
    samples = np.frombuffer(pcm, dtype=np.int16)
    voiced = np.flatnonzero(np.abs(samples) > 1500)
    return int(voiced[-1]) * 2 if len(voiced) else len(pcm)


async def stream(voice, pcm: bytes, trailing_silence_s: float = 1.0) -> float:
    """Подать звук кадрами по 20 мс в реальном времени. Возвращает момент,
    когда был подан последний звучащий сэмпл, — конец реплики оператора."""
    frame = 640
    speech_end_byte = last_voiced_byte(pcm)
    speech_ended = None
    for offset in range(0, len(pcm), frame):
        chunk = pcm[offset:offset + frame]
        voice.feed(chunk.ljust(frame, b"\x00"))
        if speech_ended is None and offset + frame > speech_end_byte:
            speech_ended = time.monotonic()
        await asyncio.sleep(0.02)
    speech_ended = speech_ended or time.monotonic()
    for _ in range(int(trailing_silence_s / 0.02)):
        voice.feed(b"\x00" * frame)
        await asyncio.sleep(0.02)
    return speech_ended


def make_session(models, embedder):
    from app.dialog.caller import TemplateCaller
    from app.dialog.persona import PersonaState
    from app.dialog.slots import SlotMachine
    from app.domain.events import SessionMode
    from app.scenarios.loader import load_file
    from app.session.state import SessionState
    from app.voice.pipeline import VoiceSession

    scenario = load_file(LIBRARY / "fire-apartment-l2.yaml", LIBRARY)
    state = SessionState(
        session_id=uuid4(), scenario_id=scenario.id, scenario_title=scenario.title,
        level=scenario.level.value, mode=SessionMode.TRAINING,
    )
    state.slots = SlotMachine(scenario, embedder)
    state.persona = PersonaState(scenario.persona)
    state.caller = TemplateCaller()

    sent: list[tuple[float, object]] = []
    voice = VoiceSession(
        session_id=state.session_id, state=state, models=models,
        send_event=lambda e: sent.append((time.monotonic(), e)),
        send_observer=lambda e: None,
        send_audio=lambda pcm: sent.append((time.monotonic(), pcm)),
    )
    return voice, state, sent


async def test_operator_asks_address_caller_answers_by_voice(models, embedder, tmp_path, monkeypatch):
    # Пустой кэш синтеза: иначе ответ звонящего берётся с диска от прошлого
    # прогона, и в замере нет настоящего синтеза.
    monkeypatch.setattr("app.voice.pipeline.CACHE", tmp_path)
    voice, state, sent = make_session(models, embedder)
    try:
        speech_ended = await stream(voice, operator_speech(models, "Назовите адрес, пожалуйста."))
        await wait_for(lambda: any(isinstance(e, TtsEnd) for _, e in sent), timeout=15)
    finally:
        await voice.close()

    heard = next(e.text for _, e in sent if isinstance(e, SttFinal))
    assert re.search(r"адрес", heard.lower()), f"распознано «{heard}»"

    reply = next(e for _, e in sent if isinstance(e, CallerUtterance))
    assert "Ленина" in reply.text, f"звонящий не назвал адрес: «{reply.text}»"

    first_audio = next(t for t, e in sent if isinstance(e, bytes))
    latency_ms = (first_audio - speech_ended) * 1000
    timing = voice.timings[-1]
    waited_ms = latency_ms - timing.speech_end_to_audio_ms
    print(f"\nконец речи оператора → первый звук звонящего: {latency_ms:.0f} мс: "
          f"ожидание паузы {waited_ms:.0f}, STT {timing.stt_ms:.0f}, звонящий {timing.caller_ms:.0f}, "
          f"TTS {timing.tts_first_ms:.0f}{', с филлером' if timing.filler else ''}")
    assert timing.tts_first_ms > 20, "синтез взят из кэша — замер нечестный"
    # DoD: ≤ 1.5 с. Без LLM звонящий отвечает заготовкой — бюджет LLM здесь не расходуется.
    assert latency_ms <= 1500, f"{latency_ms:.0f} мс"

    begin = next(t for t, e in sent if isinstance(e, TtsBegin))
    end = next(t for t, e in sent if isinstance(e, TtsEnd))
    audio_s = sum(len(e) for _, e in sent if isinstance(e, bytes)) / 2 / 24_000
    assert end - begin >= audio_s * 0.9, "tts.end пришёл раньше, чем звук мог доиграть"


async def test_operator_interrupts_caller(models, embedder):
    """Barge-in: оператор заговорил, пока звонящий говорит, — звук гасится."""
    voice, state, sent = make_session(models, embedder)
    try:
        # Реплика по инициативе звонящего — так запускается первая фраза звонка.
        speaking = voice.speak(
            "Алло! Алло! Помогите! У нас горит балкон, дым идёт в квартиру, "
            "жена с ребёнком в дальней комнате, быстрее приезжайте!", state.persona.mood)
        await wait_for(lambda: any(isinstance(e, bytes) for _, e in sent), timeout=15)
        await asyncio.sleep(0.5)

        interrupt = operator_speech(models, "Подождите, успокойтесь.")
        first_frame = time.monotonic()
        streaming = asyncio.create_task(stream(voice, interrupt, trailing_silence_s=0.2))
        await wait_for(lambda: any(isinstance(e, TtsCancel) for _, e in sent), timeout=5)
        cancelled_at = next(t for t, e in sent if isinstance(e, TtsCancel))
        await streaming
        with pytest.raises(asyncio.CancelledError):
            await speaking
    finally:
        await voice.close()

    # Первые кадры синтезированной фразы — тишина до начала звука; меряем от
    # первого кадра с речью, как её услышал бы VAD.
    pcm = np.frombuffer(interrupt, dtype=np.int16)
    onset_s = next(i for i in range(0, len(pcm), 320) if np.abs(pcm[i:i + 320]).max() > 1500) / 16_000
    detection_ms = (cancelled_at - first_frame - onset_s) * 1000
    print(f"\nперебивание: от начала речи оператора до tts.cancel {detection_ms:.0f} мс")
    assert not any(isinstance(e, TtsEnd) for _, e in sent), "перебитая реплика не должна заканчиваться штатно"
    assert detection_ms <= 300, f"{detection_ms:.0f} мс"


async def wait_for(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("не дождались")
