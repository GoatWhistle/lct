"""Локальная WAV-запись обеих сторон учебного голосового вызова."""

import logging
import os
import struct
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import numpy as np

from app.config import get_settings

TARGET_RATE = 16_000
JOURNAL_MAGIC = b"LCTREC01"
JOURNAL_RECORD = struct.Struct("<QI")  # sample offset, mono sample count
MAX_JOURNAL_RECORD_SAMPLES = TARGET_RATE * 60
log = logging.getLogger(__name__)


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
        self._segments: list[_Segment] = []
        self._finalized = False
        self._journal_path = path.with_suffix(path.suffix + ".journal")
        self._journal = None
        self._journal_failed = False
        self._started = clock()
        self._last_sync = self._started
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open_journal()

    def _open_journal(self) -> None:
        """Open or recover the append-only audio journal after process restart."""
        if self._journal_path.exists():
            valid_end = len(JOURNAL_MAGIC)
            with self._journal_path.open("r+b") as source:
                if source.read(len(JOURNAL_MAGIC)) != JOURNAL_MAGIC:
                    raise ValueError("invalid call recording journal")
                while True:
                    record = source.read(JOURNAL_RECORD.size)
                    if not record:
                        break
                    if len(record) != JOURNAL_RECORD.size:
                        break
                    offset, count = JOURNAL_RECORD.unpack(record)
                    if not count or count > MAX_JOURNAL_RECORD_SAMPLES:
                        raise ValueError("invalid call recording journal record")
                    payload = source.read(count * 2)
                    if len(payload) != count * 2:
                        break
                    samples = np.frombuffer(payload, dtype="<i2").astype(np.int32)
                    self._segments.append(_Segment(int(offset), samples))
                    valid_end = source.tell()
                source.truncate(valid_end)
                os.fsync(source.fileno())
        else:
            self._journal_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._journal_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, JOURNAL_MAGIC)
                os.fsync(fd)
            finally:
                os.close(fd)

        end = max((item.offset + item.samples.size for item in self._segments), default=0)
        if end:
            # Monotonic clocks do not survive a host reboot. Continue directly
            # after the last committed sample rather than using stale timestamps.
            self._started -= end / TARGET_RATE
        self._journal = self._journal_path.open("ab", buffering=0)
        os.chmod(self._journal_path, 0o600)

    def _write_journal_record(self, offset: int, samples: np.ndarray, now: float) -> None:
        if self._journal is None or self._journal_failed:
            return
        payload = JOURNAL_RECORD.pack(offset, int(samples.size)) + samples.astype("<i2").tobytes()
        try:
            view = memoryview(payload)
            while view:
                written = self._journal.write(view)
                if not written:
                    raise OSError("short audio journal write")
                view = view[written:]
            if now - self._last_sync >= 1.0:
                os.fsync(self._journal.fileno())
                self._last_sync = now
        except OSError:
            # Keep the live call working; finalize can still save the in-memory
            # audio. The warning is explicit because crash recovery is degraded.
            self._journal_failed = True
            log.error("журнал WAV недоступен (%s)", self._journal_path.name)
            self._journal.close()
            self._journal = None

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
        now = self._clock()
        offset = max(0, round((now - self._started) * TARGET_RATE))
        self._write_journal_record(offset, source, now)
        self._segments.append(_Segment(offset=offset, samples=source))

    def finalize(self) -> Path | None:
        if self._finalized:
            return self.path if self.path.is_file() else None
        self._finalized = True
        if not self._segments:
            self._close_journal(remove=True)
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
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        self._close_journal(remove=True)
        return self.path

    def _close_journal(self, *, remove: bool) -> None:
        if self._journal is not None:
            try:
                os.fsync(self._journal.fileno())
            except OSError as exc:
                log.warning("не удалось синхронизировать журнал WAV (%s)", type(exc).__name__)
            finally:
                self._journal.close()
                self._journal = None
        if remove:
            try:
                self._journal_path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("не удалось удалить журнал WAV (%s)", type(exc).__name__)


def start_recording(session_id: UUID) -> CallRecorder | None:
    if not get_settings().record_calls:
        return None
    try:
        return CallRecorder(recording_path(session_id))
    except (OSError, ValueError) as exc:
        # Recording failure must not drop an otherwise recoverable call.
        log.error("сессия %s: запись звонка недоступна (%s)", session_id, type(exc).__name__)
        return None
