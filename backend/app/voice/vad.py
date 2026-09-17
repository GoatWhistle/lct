"""Потоковый VAD: где оператор начал говорить и где закончил.

Silero VAD, окно 32 мс при 16 кГц, меньше миллисекунды на окно (docs/LATENCY.md).
Основная задержка не в модели, а в endpointing: фраза считается законченной
после 600 мс тишины. Ниже 400 мс режет на паузах внутри фразы («улица...
эээ... Ленина»), выше 800 мс ощущается как тормоз — не подбирать заново
(docs/arch/STACK.md).
"""

from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

RATE = 16_000
WINDOW = 512  # 32 мс
CONTEXT = 64  # хвост предыдущего окна: так модель обучалась, без него точность падает
WINDOW_MS = WINDOW * 1000 // RATE


@dataclass
class SpeechStarted:
    """Оператор заговорил. Если звонящий в этот момент говорит — это barge-in."""


@dataclass
class SpeechEnded:
    audio: np.ndarray  # float32, 16 кГц, с предзахватом начала фразы


class StreamingVad:
    def __init__(
        self,
        model_path: Path,
        *,
        start_threshold: float = 0.5,
        end_threshold: float = 0.35,
        endpointing_ms: int = 600,
        min_speech_ms: int = 64,
        preroll_ms: int = 200,
        max_utterance_ms: int = 20_000,
    ) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = 1  # окно — доли миллисекунды, потоки только мешают
        self._session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        # Порог начала выше порога конца: гистерезис, чтобы фраза не рвалась
        # на каждом тихом слоге.
        self.start_threshold = start_threshold
        self.end_threshold = end_threshold
        self.endpointing_ms = endpointing_ms
        self.min_speech_ms = min_speech_ms
        self.max_utterance_ms = max_utterance_ms

        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        # Предзахват: первые звуки фразы звучат до того, как VAD уверится,
        # что это речь. Без него «Назовите» распознаётся как «зовите».
        self._preroll: deque[np.ndarray] = deque(maxlen=max(1, preroll_ms // WINDOW_MS))
        self._speech: list[np.ndarray] = []
        self._voiced_ms = 0
        self._silence_ms = 0
        self._in_speech = False

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def _probability(self, window: np.ndarray) -> float:
        frame = np.concatenate([self._context, window])[None, :]
        output, self._state = self._session.run(
            None, {"input": frame, "state": self._state, "sr": np.array(RATE, dtype=np.int64)}
        )
        self._context = window[-CONTEXT:]
        return float(output[0][0])

    def push(self, pcm16: bytes) -> list[SpeechStarted | SpeechEnded]:
        """Кадр PCM16 16 кГц любой длины → события."""
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768
        self._pending = np.concatenate([self._pending, samples])
        events: list[SpeechStarted | SpeechEnded] = []

        while len(self._pending) >= WINDOW:
            window, self._pending = self._pending[:WINDOW], self._pending[WINDOW:]
            probability = self._probability(window)

            if not self._in_speech:
                self._preroll.append(window)
                self._voiced_ms = self._voiced_ms + WINDOW_MS if probability >= self.start_threshold else 0
                if self._voiced_ms >= self.min_speech_ms:
                    self._in_speech = True
                    self._speech = list(self._preroll)
                    self._silence_ms = 0
                    events.append(SpeechStarted())
                continue

            self._speech.append(window)
            self._silence_ms = self._silence_ms + WINDOW_MS if probability < self.end_threshold else 0
            too_long = len(self._speech) * WINDOW_MS >= self.max_utterance_ms
            if self._silence_ms >= self.endpointing_ms or too_long:
                # Хвост тишины распознаванию не нужен, но короткий запас оставляем:
                # конец последнего слова бывает тише порога.
                keep = len(self._speech) - max(0, self._silence_ms - 200) // WINDOW_MS
                events.append(SpeechEnded(audio=np.concatenate(self._speech[:keep])))
                self._in_speech = False
                self._speech = []
                self._voiced_ms = 0
                self._preroll.clear()
        return events
