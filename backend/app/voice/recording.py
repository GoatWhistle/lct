"""Локальная WAV-запись обеих сторон учебного голосового вызова."""

import os
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import numpy as np

from app.config import get_settings

TARGET_RATE = 16_000


def recording_path(session_id: UUID) -> Path:
    return Path(get_settings().recordings_dir).resolve() / f"{session_id}.wav"


@dataclass(frozen=True)
class _Segment:
    offset: int
    samples: np.ndarray


class CallRecorder:
    """Смешивает PCM16 разных частот на монотаймлайн 16 кГц.

    Вход курсанта приходит по 16 кГц, TTS звонящего — по 24 кГц. Метка
    monotonic сохраняет паузы и взаимное расположение реплик; системные часы
    и изменение времени на хосте на запись не влияют.
    """

    def __init__(self, path: Path, *, clock=time.monotonic) -> None:
        self.path = path
        self._clock = clock
        self._started = clock()
        self._segments: list[_Segment] = []
        self._finalized = False

    def add_pcm(self, pcm: bytes, *, sample_rate: int) -> None:
        if self._finalized or not pcm or sample_rate <= 0 or len(pcm) % 2:
            return
        source = np.frombuffer(pcm, dtype="<i2").astype(np.int32)
        if source.size == 0:
            return
        if sample_rate != TARGET_RATE:
            length = max(1, round(source.size * TARGET_RATE / sample_rate))
            points = np.linspace(0, source.size - 1, length)
            source = np.rint(np.interp(points, np.arange(source.size), source)).astype(np.int32)
        offset = max(0, round((self._clock() - self._started) * TARGET_RATE))
        self._segments.append(_Segment(offset=offset, samples=source))

    def finalize(self) -> Path | None:
        if self._finalized:
            return self.path if self.path.is_file() else None
        self._finalized = True
        if not self._segments:
            return None
        total = max(item.offset + item.samples.size for item in self._segments)
        mixed = np.zeros(total, dtype=np.int32)
        for item in self._segments:
            mixed[item.offset:item.offset + item.samples.size] += item.samples
        pcm = np.clip(mixed, -32768, 32767).astype("<i2").tobytes()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".wav.tmp")
        with wave.open(str(temporary), "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(TARGET_RATE)
            target.writeframes(pcm)
        os.replace(temporary, self.path)
        return self.path


def start_recording(session_id: UUID) -> CallRecorder | None:
    if not get_settings().record_calls:
        return None
    return CallRecorder(recording_path(session_id))
