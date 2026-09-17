"""Реестр живых сессий и подписки наблюдателей.

Дельты курсанту, полное состояние наблюдателям — сознательно: у курсанта одно
соединение и важна задержка, наблюдателей несколько и подключаются они
в произвольный момент (docs/arch/CONTRACT.md).
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from app.domain.events import TimerTick
from app.session.state import SessionState

#: Очередь одного подписчика. Медленный наблюдатель не тормозит занятие:
#: очередь ограничена, переполнение роняет соединение, а не сессию.
QUEUE_SIZE = 256

TICK_SECONDS = 1.0


class Journal(Protocol):
    """Запись в БД. Вынесена за хаб: без базы занятие должно идти,
    но молчать об ошибке записи нельзя."""

    async def start_lesson(
        self, session_id: UUID, scenario_id: str, mode: str, trainee_name: str | None
    ) -> int: ...
    async def utterance(self, session_id: UUID, entry) -> None: ...
    async def hint(self, session_id: UUID, checklist_id: str, question: str, at) -> None: ...
    async def note(self, session_id: UUID, ref: str, text: str, author: str) -> None: ...
    async def self_assessment(self, session_id: UUID, missed: list[str], comment: str, at) -> None: ...
    async def score(self, session_id: UUID, score_auto: float, report: dict) -> None: ...
    async def session_started(self, session_id: UUID, at) -> None: ...
    async def session_ended(self, session_id: UUID, at, reason: str) -> None: ...


class SessionHub:
    def __init__(self, journal: Journal | None = None) -> None:
        self.journal = journal
        self._sessions: dict[UUID, SessionState] = {}
        self._observers: dict[UUID, set[asyncio.Queue]] = {}
        self._trainees: dict[UUID, set[asyncio.Queue]] = {}
        self._tickers: dict[UUID, asyncio.Task] = {}

    # ── реестр ──

    def register(self, state: SessionState) -> SessionState:
        self._sessions[state.session_id] = state
        return state

    def get(self, session_id: UUID) -> SessionState | None:
        return self._sessions.get(session_id)

    def drop(self, session_id: UUID) -> None:
        self._sessions.pop(session_id, None)
        self.stop_ticker(session_id)

    # ── подписки ──

    @contextlib.contextmanager
    def _subscribe(self, registry: dict[UUID, set[asyncio.Queue]], session_id: UUID) -> Iterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        registry.setdefault(session_id, set()).add(queue)
        try:
            yield queue
        finally:
            registry.get(session_id, set()).discard(queue)

    def observer(self, session_id: UUID):
        return self._subscribe(self._observers, session_id)

    def trainee(self, session_id: UUID):
        return self._subscribe(self._trainees, session_id)

    # ── вещание ──

    @staticmethod
    def _put(queues: set[asyncio.Queue], event: BaseModel | bytes) -> None:
        for queue in list(queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                queues.discard(queue)

    def to_observers(self, session_id: UUID, event: BaseModel) -> None:
        self._put(self._observers.get(session_id, set()), event)

    def to_trainee(self, session_id: UUID, event: BaseModel | bytes) -> None:
        self._put(self._trainees.get(session_id, set()), event)

    def broadcast(self, session_id: UUID, event: BaseModel) -> None:
        self.to_trainee(session_id, event)
        self.to_observers(session_id, event)

    def observer_count(self, session_id: UUID) -> int:
        return len(self._observers.get(session_id, set()))

    # ── такт таймеров ──

    def start_ticker(self, session_id: UUID) -> None:
        """`timer.tick` раз в секунду, а не на каждое изменение: таймеров дюжина,
        а UI всё равно рисует секунды."""
        if session_id in self._tickers:
            return
        self._tickers[session_id] = asyncio.create_task(self._tick(session_id))

    def stop_ticker(self, session_id: UUID) -> None:
        task = self._tickers.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def shutdown(self) -> None:
        """Погасить все такты при остановке приложения.

        Без этого задачи тикеров переживают выключение и держат событийный цикл:
        первым это ловит не продакшен, а тест, который не может закрыть клиент.
        """
        for session_id in list(self._tickers):
            self.stop_ticker(session_id)

    async def _tick(self, session_id: UUID) -> None:
        try:
            while True:
                await asyncio.sleep(TICK_SECONDS)
                state = self.get(session_id)
                if state is None or state.ended:
                    return
                self.broadcast(session_id, TimerTick(timers=state.timers.snapshot()))
        except asyncio.CancelledError:
            raise


hub = SessionHub()


async def drain(queue: asyncio.Queue) -> AsyncIterator[BaseModel]:
    while True:
        yield await queue.get()
