"""Голосовой контур одного звонка: VAD → STT → звонящий → TTS, с перебиванием.

Задача ответа живёт, пока у курсанта **доигрывает звук**, а не только пока идёт
синтез. Поэтому barge-in — это просто отмена этой задачи: одним движением гасятся
распознавание, реплика звонящего, синтез и ожидание конца воспроизведения
(docs/arch/BACKEND.md), а `tts.end` приходит тогда, когда звонящий действительно
замолчал.
"""

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import numpy as np

from app.config import get_settings
from app.domain.events import (
    CallerUtterance,
    Mood,
    Speaker,
    SttFinal,
    TranscriptAppend,
    TtsBegin,
    TtsCancel,
    TtsEnd,
)
from app.voice.models import TTS_RATE, VoiceModels
from app.voice.text import sentences, speech_chunks
from app.voice.vad import SpeechEnded, SpeechStarted, StreamingVad

log = logging.getLogger(__name__)

#: Если ответ не готов за секунду — звонящий «переспрашивает». Маскирует паузу
#: и сюжетно оправдано для паникующего: это поведение персонажа, а не костыль.
FILLER_AFTER_S = 1.0
FILLERS: dict[Mood, str] = {
    Mood.PANIC: "Алло?! Вы тут?!",
    Mood.AGGRESSIVE: "Алло! Вы там уснули?!",
    Mood.WORRIED: "Алло?..",
    Mood.CALM: "Алло?",
    Mood.CONFUSED: "Алло... кто это?",
}

CACHE = Path(__file__).resolve().parents[2] / get_settings().models_dir / "cache" / "tts"


@dataclass
class TurnTiming:
    """Разбивка задержки одного хода — то, что меряет DoD «≤ 1.5 с»."""

    stt_ms: float = 0.0
    caller_ms: float = 0.0
    tts_first_ms: float = 0.0
    speech_end_to_audio_ms: float = 0.0
    filler: bool = False


@dataclass
class VoiceSession:
    session_id: UUID
    state: object  # SessionState; без импорта, чтобы не завязать сессию на голос
    models: VoiceModels
    send_event: Callable[[object], None]
    send_observer: Callable[[object], None]
    send_audio: Callable[[bytes], None]
    journal: object | None = None

    timings: list[TurnTiming] = field(default_factory=list)
    _vad: StreamingVad = field(init=False)
    _queue: asyncio.Queue = field(init=False)
    _worker: asyncio.Task | None = field(init=False, default=None)
    _reply: asyncio.Task | None = field(init=False, default=None)
    _utterance_id: UUID | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._vad = StreamingVad(self.models.vad_path, endpointing_ms=get_settings().endpointing_ms)
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._work())

    # ── вход: кадры микрофона ──

    def feed(self, frame: bytes) -> None:
        for event in self._vad.push(frame):
            if isinstance(event, SpeechStarted) and self.speaking:
                self.barge_in()
            elif isinstance(event, SpeechEnded):
                self._queue.put_nowait((event.audio, time.monotonic()))

    @property
    def speaking(self) -> bool:
        return self._reply is not None and not self._reply.done()

    def speak(self, text: str, mood: Mood) -> asyncio.Task:
        """Реплика по инициативе звонящего — первая фраза, директива.

        Запускается только так, а не прямым `say()`: перебивание отменяет
        `self._reply`, и реплика мимо него была бы неперебиваемой. Именно так
        первая фраза «Алло! Помогите!» — ровно та, которую оператор перебивает
        чаще всего, — не гасилась вовсе.
        """
        if self.speaking:
            self._reply.cancel()
        self._reply = asyncio.create_task(self.say(text, mood))
        return self._reply

    def barge_in(self) -> None:
        """Оператор перебил. Сервер — авторитет: гасит всё и говорит фронту
        выбросить недоигранный звук."""
        if not self.speaking:
            return
        self._reply.cancel()
        if self._utterance_id is not None:
            self.send_event(TtsCancel(utterance_id=self._utterance_id, reason="barge_in"))
        log.info("сессия %s: перебивание", self.session_id)

    async def close(self) -> None:
        for task in (self._reply, self._worker):
            if task is not None:
                task.cancel()

    # ── ответ звонящего ──

    async def _work(self) -> None:
        while True:
            audio, ended_at = await self._queue.get()
            # Новая фраза оператора, пока звонящий ещё говорит, — тоже перебивание.
            if self.speaking:
                self.barge_in()
            self._reply = asyncio.create_task(self._respond(audio, ended_at))
            try:
                await self._reply
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise  # отменили сам контур, а не ответ

    async def _respond(self, audio: np.ndarray, ended_at: float) -> None:
        timing = TurnTiming()
        started = time.monotonic()
        text = await self.models.transcribe(audio)
        timing.stt_ms = (time.monotonic() - started) * 1000
        if not text:
            return

        entry = self.state.append(Speaker.OPERATOR, text)
        self.send_event(SttFinal(text=text, at=entry.at))
        self.send_observer(TranscriptAppend(entry=entry))
        if self.journal:
            await self.journal.utterance(self.session_id, entry)

        started = time.monotonic()
        line = self._caller_line(text)
        timing.caller_ms = (time.monotonic() - started) * 1000
        await self.say(line.text, line.mood, ended_at=ended_at, timing=timing)

    def _caller_line(self, text: str):
        state = self.state
        if state.slots is None or state.caller is None or state.persona is None:
            from app.dialog.caller import CallerLine

            return CallerLine(text=FILLERS[Mood.PANIC], mood=Mood.PANIC)
        turn = state.slots.hear(text)
        return state.caller.reply(turn, state.persona, state.slots)

    async def say(
        self, text: str, mood: Mood, *, ended_at: float | None = None, timing: TurnTiming | None = None
    ) -> None:
        """Произнести реплику: событие с текстом, звук по предложениям, ожидание конца."""
        self._utterance_id = utterance_id = uuid4()
        entry = self.state.append(Speaker.CALLER, text, mood)
        self.send_event(CallerUtterance(utterance_id=utterance_id, text=text, at=entry.at, mood=mood))
        self.send_observer(TranscriptAppend(entry=entry))
        if self.journal:
            await self.journal.utterance(self.session_id, entry)

        self.send_event(TtsBegin(utterance_id=utterance_id))
        playback_ends = time.monotonic()
        for index, sentence in enumerate(speech_chunks(text)):
            synth_started = time.monotonic()
            if index == 0 and ended_at is not None:
                pcm = await self._first_sentence(sentence, mood, ended_at, timing)
            else:
                pcm = await self.synthesize(sentence)
            if index == 0 and timing is not None:
                timing.tts_first_ms = (time.monotonic() - synth_started) * 1000
            if not pcm:
                continue
            if index == 0 and ended_at is not None and timing is not None:
                timing.speech_end_to_audio_ms = (time.monotonic() - ended_at) * 1000
            self.send_audio(pcm)
            playback_ends = max(playback_ends, time.monotonic()) + len(pcm) / 2 / TTS_RATE

        if timing is not None:
            self.timings.append(timing)
            log.info(
                "сессия %s: ответ через %.0f мс после конца фразы (STT %.0f, звонящий %.0f, TTS %.0f%s)",
                self.session_id, timing.speech_end_to_audio_ms, timing.stt_ms,
                timing.caller_ms, timing.tts_first_ms, ", с филлером" if timing.filler else "",
            )
        # Ждём, пока курсант дослушает: перебивание в это время отменит задачу.
        await asyncio.sleep(max(0.0, playback_ends - time.monotonic()))
        self.send_event(TtsEnd(utterance_id=utterance_id))

    async def _first_sentence(
        self, sentence: str, mood: Mood, ended_at: float, timing: TurnTiming | None
    ) -> bytes:
        """Первое предложение с филлером: если к секунде после конца фразы звука
        ещё нет, звонящий «переспрашивает», а ответ встанет в очередь за ним."""
        synthesis = asyncio.ensure_future(self.synthesize(sentence))
        remaining = FILLER_AFTER_S - (time.monotonic() - ended_at)
        if remaining > 0:
            done, _ = await asyncio.wait({synthesis}, timeout=remaining)
            if done:
                return synthesis.result()
        filler = await self.synthesize(FILLERS.get(mood, FILLERS[Mood.PANIC]))
        if filler:
            self.send_audio(filler)
            if timing is not None:
                timing.filler = True
        return await synthesis

    async def synthesize(self, text: str) -> bytes:
        return await cached_synthesize(self.models, text)


async def cached_synthesize(models: VoiceModels, text: str) -> bytes:
    """Синтез с кэшем на диске: первая реплика и филлеры звучат мгновенно,
    а повторные фразы не синтезируются заново."""
    key = hashlib.sha1(f"{models.synthesizer.speaker}|{TTS_RATE}|{text}".encode()).hexdigest()
    path = CACHE / f"{key}.pcm"
    if path.exists():
        return path.read_bytes()
    pcm = await models.synthesize(text)
    if pcm:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pcm)
    return pcm


async def prefetch(models: VoiceModels, texts: list[str]) -> None:
    """Заранее синтезировать первую реплику и филлеры, пока курсант не снял трубку."""
    for text in texts:
        for sentence in sentences(text):
            await cached_synthesize(models, sentence)
