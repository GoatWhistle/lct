"""Модели речи процесса: грузятся один раз, общие для всех сессий.

Инференс занимает процессор на сотни миллисекунд и идёт в отдельном пуле потоков:
в событийном цикле он остановил бы все сокеты всех экранов занятия.
onnxruntime и torch отпускают GIL на время вычислений, так что потоки работают параллельно.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import numpy as np

from app.config import get_settings
from app.voice.text import normalize

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
TTS_RATE = 24_000
#: Потоков на распознавание и на синтез. Вместе — меньше числа ядер, иначе
#: модели, идущие друг за другом, вытесняют друг друга.
INFERENCE_THREADS = 4

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice")


class Recognizer:
    """GigaAM v3 RNNT int8: ~285 мс на фразу в 2 с, WER 0% на чистой речи (docs/LATENCY.md)."""

    def __init__(self, model_dir: Path) -> None:
        import onnx_asr
        import onnxruntime as ort

        options = ort.SessionOptions()
        # По умолчанию onnxruntime берёт все ядра и вытесняет синтез, который
        # идёт следом: в контуре распознавание выходило вдвое медленнее замера.
        options.intra_op_num_threads = INFERENCE_THREADS
        self._model = onnx_asr.load_model(
            "gigaam-v3-rnnt", model_dir, quantization="int8", sess_options=options
        )

    def transcribe(self, audio: np.ndarray) -> str:
        return self._model.recognize(audio, sample_rate=16_000).strip()

    def warmup(self) -> None:
        """Первый вызов холодный: выделение памяти, подготовка графа."""
        rng = np.random.default_rng(0)
        self.transcribe((rng.normal(0, 0.05, 16_000)).astype(np.float32))


class Synthesizer:
    """Silero v5: около десятой доли длительности фразы на синтез (docs/LATENCY.md)."""

    def __init__(self, model_path: Path, speaker: str = "xenia") -> None:
        import torch

        torch.set_num_threads(INFERENCE_THREADS)
        # Профилирующий компилятор на новых длинах входа не выигрывает: без него −13%.
        torch._C._jit_set_profiling_executor(False)
        importer = torch.package.PackageImporter(str(model_path))
        self._model = importer.load_pickle("tts_models", "model")
        self._model.to(torch.device("cpu"))
        self.speaker = speaker

    def synthesize(self, text: str) -> bytes:
        """PCM16 24 кГц. Пустая реплика — пустые байты: Silero на пустой строке падает."""
        spoken = normalize(text)
        if not spoken:
            return b""
        audio = self._model.apply_tts(text=spoken, speaker=self.speaker, sample_rate=TTS_RATE)
        pcm = (audio.clamp(-1, 1).numpy() * 32767).astype(np.int16)
        return pcm.tobytes()

    def warmup(self) -> None:
        for text in ("Алло!", "Улица Ленина, дом четырнадцать, квартира сорок семь."):
            self.synthesize(text)


class VoiceModels:
    def __init__(self, recognizer: Recognizer, synthesizer: Synthesizer, vad_path: Path) -> None:
        self.recognizer = recognizer
        self.synthesizer = synthesizer
        self.vad_path = vad_path

    async def transcribe(self, audio: np.ndarray) -> str:
        return await asyncio.get_running_loop().run_in_executor(_pool, self.recognizer.transcribe, audio)

    async def synthesize(self, text: str) -> bytes:
        return await asyncio.get_running_loop().run_in_executor(_pool, self.synthesizer.synthesize, text)


@lru_cache(maxsize=1)
def get_voice_models() -> VoiceModels | None:
    """None, если голос выключен или моделей нет: занятие идёт без голоса,
    и это видно в /api/health, а не падением на первом звонке."""
    settings = get_settings()
    if not settings.voice_enabled:
        return None
    models = ROOT / settings.models_dir
    required = [
        models / "gigaam-v3-onnx" / "v3_rnnt_encoder.int8.onnx",
        models / "silero-vad" / "silero_vad.onnx",
        models / "silero-tts" / "v5_ru.pt",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    if missing:
        log.warning("голосовой контур выключен, нет моделей: %s — make models", ", ".join(missing))
        return None
    try:
        recognizer = Recognizer(models / "gigaam-v3-onnx")
        synthesizer = Synthesizer(models / "silero-tts" / "v5_ru.pt")
        # Прогрев здесь, на старте стенда, а не на первой реплике курсанта.
        recognizer.warmup()
        synthesizer.warmup()
        return VoiceModels(recognizer, synthesizer, models / "silero-vad" / "silero_vad.onnx")
    except ImportError as exc:
        log.warning("голосовой контур выключен: не установлены зависимости (%s) — uv sync --extra voice", exc)
        return None
