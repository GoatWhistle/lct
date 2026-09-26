"""Реестр живых сессий и подписки наблюдателей.

Дельты курсанту, полное состояние наблюдателям — сознательно: у курсанта одно
соединение и важна задержка, наблюдателей несколько и подключаются они
в произвольный момент (docs/arch/CONTRACT.md).
"""

import asyncio
import contextlib
import logging
from contextvars import ContextVar, Token
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from app.domain.events import (
    CardReceived,
    ErrorEvent,
    ErrorKind,
    Exercise,
    StationState,
    TimerTick,
)
from app.session.state import SessionState

#: Очередь одного подписчика. Медленный наблюдатель не тормозит занятие:
#: очередь ограничена, переполнение роняет соединение, а не сессию.
QUEUE_SIZE = 256

TICK_SECONDS = 1.0
LEASE_FENCED_MESSAGE = "Занятие передано другому backend-узлу; переподключитесь."
log = logging.getLogger(__name__)


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:  # synchronous tests and tooling have no running loop
        return None


class Journal(Protocol):
    """Запись в БД. Вынесена за хаб: без базы занятие должно идти,
    но молчать об ошибке записи нельзя."""

    async def start_lesson(
        self, session_id: UUID, scenario_id: str, mode: str, trainee_name: str | None,
        trainee_id: UUID | None = None, owner_login: str | None = None,
        backend_node_id: str | None = None,
    ) -> tuple[int, UUID | None, str | None, int] | None: ...
    async def utterance(self, session_id: UUID, entry) -> None: ...
    async def hint(self, session_id: UUID, checklist_id: str, question: str, at) -> None: ...
    async def note(self, session_id: UUID, ref: str, text: str, author: str) -> None: ...
    async def self_assessment(
        self, session_id: UUID, missed: list[str], comment: str, at
    ) -> bool: ...
    async def score(self, session_id: UUID, score_auto: float, report: dict) -> bool: ...
    async def score_snapshot(self, session_id: UUID, report: dict) -> None: ...
    async def score_override(
        self, session_id: UUID, score_final: float, author: str, comment: str,
    ) -> bool: ...
    async def session_started(self, session_id: UUID, at) -> None: ...
    async def session_ended(self, session_id: UUID, at, reason: str) -> None: ...
    async def checkpoint(self, state: SessionState) -> None: ...
    async def restore_active(self) -> list[SessionState]: ...
    async def renew(self, session_id: UUID) -> None: ...
    async def claim_expired(self, session_id: UUID | None = None) -> list[SessionState]: ...


class SessionHub:
    def __init__(self, journal: Journal | None = None) -> None:
        self.journal = journal
        self._sessions: dict[UUID, SessionState] = {}
        self._observers: dict[UUID, set[asyncio.Queue]] = {}
        self._trainees: dict[UUID, set[asyncio.Queue]] = {}
        self._stations: dict[UUID, set[asyncio.Queue]] = {}
        self._tickers: dict[UUID, asyncio.Task] = {}
        self._event_batch: ContextVar[dict | None] = ContextVar(
            f"session-event-batch-{id(self)}", default=None
        )

    # ── реестр ──

    def register(self, state: SessionState) -> SessionState:
        self._sessions[state.session_id] = state
        return state

    def get(self, session_id: UUID) -> SessionState | None:
        state = self._sessions.get(session_id)
        return None if state is not None and state.lease_fenced else state

    def is_lease_fenced(self, session_id: UUID) -> bool:
        state = self._sessions.get(session_id)
        return state is not None and state.lease_fenced

    def active_sessions(self, owner_login: str) -> list[SessionState]:
        """Живые занятия только преподавателя-владельца для группового обзора."""
        return [
            state for state in self._sessions.values()
            if not state.ended and not state.lease_fenced and state.owner_login == owner_login
        ]

    def history(
        self, *, owner_login: str | None = None, trainee_id: UUID | None = None,
        mode: str | None = None, since: datetime | None = None, limit: int = 100,
    ) -> list[SessionState]:
        """Volatile session history for the explicit no-database demo mode."""
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        states = [
            state for state in self._sessions.values()
            if not state.lease_fenced
            and (owner_login is None or state.owner_login == owner_login)
            and (trainee_id is None or state.trainee_id == trainee_id)
            and (mode is None or state.mode.value == mode)
            and (since is None or (state.started_at is not None and state.started_at >= since))
        ]
        # Hub insertion order is creation order; completed lessons sort by
        # their finish time, while unanswered calls retain their start time.
        states.sort(
            key=lambda state: state.ended_at or state.started_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return states[:max(0, limit)]

    def has_active_scenario(self, scenario_id: str) -> bool:
        """Архивирование контента не должно менять уже идущее занятие."""
        return any(
            not state.ended and not state.lease_fenced and (
                state.scenario_id == scenario_id
                or any(item.id == scenario_id for item in state.dds_scenarios)
            )
            for state in self._sessions.values()
        )

    def drop(self, session_id: UUID) -> None:
        self._sessions.pop(session_id, None)
        self.stop_ticker(session_id)

    async def checkpoint(self, session_id: UUID) -> None:
        """Зафиксировать подтверждённое состояние, если журнал доступен."""
        state = self._sessions.get(session_id)
        if state is not None and state.lease_fenced:
            raise RuntimeError(LEASE_FENCED_MESSAGE)
        if state is not None and self.journal is not None:
            try:
                await self.journal.checkpoint(state)
            except Exception:
                self._discard_event_batch(session_id)
                await self.fence(state)
                raise
        self._flush_event_batch(session_id)

    def begin_event_stream(self, session_id: UUID) -> Token:
        """Stage controller output until each explicit checkpoint in its loop."""
        return self._event_batch.set({
            "session_id": session_id, "events": [], "committed": False,
            "persistent": True, "owner_task": _current_task(),
        })

    async def end_event_stream(self, token: Token) -> None:
        batch = self._event_batch.get()
        try:
            if batch is not None and batch["events"]:
                session_id = batch["session_id"]
                batch["events"].clear()
                state = self._sessions.get(session_id)
                if self.journal is not None and state is not None and not state.lease_fenced:
                    await self.fence(state)
        finally:
            self._event_batch.reset(token)

    @contextlib.asynccontextmanager
    async def durable_transition(self, session_id: UUID):
        """Do not publish state-changing events until its checkpoint commits."""
        batch = {
            "session_id": session_id, "events": [], "committed": False,
            "persistent": False, "owner_task": _current_task(),
        }
        token: Token = self._event_batch.set(batch)
        try:
            yield
            if not batch["committed"]:
                await self.checkpoint(session_id)
            else:
                self._flush_event_batch(session_id)
        except Exception:
            self._discard_event_batch(session_id)
            state = self._sessions.get(session_id)
            if self.journal is not None and state is not None and not state.lease_fenced:
                await self.fence(state)
            raise
        finally:
            self._event_batch.reset(token)

    def _discard_event_batch(self, session_id: UUID) -> None:
        batch = self._event_batch.get()
        if batch is not None and batch["session_id"] == session_id:
            batch["events"].clear()

    def _flush_event_batch(self, session_id: UUID) -> None:
        batch = self._event_batch.get()
        if batch is None or batch["session_id"] != session_id:
            return
        pending, batch["events"] = batch["events"], []
        batch["committed"] = not batch.get("persistent", False)
        for registry, target_session_id, event in pending:
            self._put(registry.get(target_session_id, set()), event)

    def _send(self, registry: dict[UUID, set[asyncio.Queue]], session_id: UUID,
              event: BaseModel) -> None:
        batch = self._event_batch.get()
        if isinstance(event, ErrorEvent):
            self._put(registry.get(session_id, set()), event)
        elif (batch is not None and batch["session_id"] == session_id
              and batch["owner_task"] is _current_task()):
            batch["events"].append((registry, session_id, event))
        else:
            self._put(registry.get(session_id, set()), event)

    async def fence(self, state: SessionState) -> None:
        """Fail closed when durable ownership is lost or cannot be confirmed."""
        if state.lease_fenced:
            return
        state.lease_fenced = True
        self.stop_ticker(state.session_id)
        if state.voice is not None:
            try:
                await state.voice.close()
            except Exception as exc:  # noqa: BLE001 — fencing must still close data channels
                log.error("не удалось закрыть голос при fencing занятия %s (%s)",
                          state.session_id, type(exc).__name__)
        event = ErrorEvent(code=ErrorKind.INTERNAL, message=LEASE_FENCED_MESSAGE)
        self.broadcast(state.session_id, event)
        self.to_station(state.session_id, event)

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

    def station(self, session_id: UUID):
        return self._subscribe(self._stations, session_id)

    # ── вещание ──

    @staticmethod
    def _put(queues: set[asyncio.Queue], event: BaseModel | bytes) -> None:
        for queue in list(queues):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                queues.discard(queue)

    def to_observers(self, session_id: UUID, event: BaseModel) -> None:
        self._send(self._observers, session_id, event)

    def to_trainee(self, session_id: UUID, event: BaseModel | bytes) -> None:
        if isinstance(event, BaseModel):
            self._send(self._trainees, session_id, event)
        else:
            self._put(self._trainees.get(session_id, set()), event)

    def to_station(self, session_id: UUID, event: BaseModel) -> None:
        self._send(self._stations, session_id, event)

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
                if state.exercise is Exercise.DDS or (state.handoff_to_dds and state.dds_scenarios):
                    from app.session.dds import deliver_due_cards

                    active_before = state.dds_active_card_id
                    delivered = deliver_due_cards(state)
                    if state.dds_active_card_id != active_before and state.dds_active_card_id:
                        self.to_station(session_id, state.card_received_event())
                    if delivered:
                        await self.checkpoint(session_id)
                    # Keep the pending count and countdown live even while
                    # the active dispatcher card is being handled.
                    self.to_station(session_id, StationState(snapshot=state.station_snapshot()))
                tick = TimerTick(timers=state.timers.snapshot())
                self.broadcast(session_id, tick)
                # Срок первичного решения пульт берёт отсюда: после передачи
                # из 112 без очереди ДДС лимит преподавателя больше нигде не виден.
                self.to_station(session_id, tick)
        except asyncio.CancelledError:
            raise


hub = SessionHub()


async def drain(queue: asyncio.Queue) -> AsyncIterator[BaseModel]:
    while True:
        yield await queue.get()
