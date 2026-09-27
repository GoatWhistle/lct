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
from uuid import UUID

from pydantic import BaseModel

from app.domain.events import (
    CardReceived,
    ErrorEvent,
    ErrorKind,
    StationState,
    TimerTick,
)
from app.session.state import SessionState
from app.session.store import MemorySessionStore, Record, SessionLeaseLost, SessionStore
from app.session.timers import now_utc

#: Очередь одного подписчика. Медленный наблюдатель не тормозит занятие:
#: очередь ограничена, переполнение роняет соединение, а не сессию.
QUEUE_SIZE = 256

TICK_SECONDS = 1.0
LEASE_FENCED_MESSAGE = "Занятие передано другому backend-узлу; переподключитесь."
log = logging.getLogger(__name__)


class _Operation:
    """Отложенная публикация одной операции: события ждут её commit."""

    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id
        self.records: list[Record] = []
        self.events: list[tuple[dict[UUID, set[asyncio.Queue]], BaseModel]] = []
        self.open = True
        #: Такт, которому нечего сохранять, не пишет снимок каждую секунду.
        self.persist = True


class SessionHub:
    def __init__(self, store: SessionStore | None = None) -> None:
        self.store: SessionStore = store if store is not None else MemorySessionStore()
        self._sessions: dict[UUID, SessionState] = {}
        self._observers: dict[UUID, set[asyncio.Queue]] = {}
        self._trainees: dict[UUID, set[asyncio.Queue]] = {}
        self._stations: dict[UUID, set[asyncio.Queue]] = {}
        self._trainee_calls: dict[UUID, set[asyncio.Queue]] = {}
        self._trainee_stations: dict[UUID, set[asyncio.Queue]] = {}
        self._tickers: dict[UUID, asyncio.Task] = {}
        # Задачи, порождённые внутри операции, наследуют контекст; после
        # закрытия операции их события идут напрямую (`_Operation.open`).
        self._operation: ContextVar[_Operation | None] = ContextVar(
            f"session-operation-{id(self)}", default=None
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
                or any(item.id == scenario_id for item in state.desk.scenarios)
            )
            for state in self._sessions.values()
        )

    def drop(self, session_id: UUID) -> None:
        self._sessions.pop(session_id, None)
        self.stop_ticker(session_id)

    def live_count(self) -> int:
        """Сколько занятий этот узел сейчас ведёт — для диагностики администратора."""
        return sum(1 for state in self._live())

    def _live(self) -> list[SessionState]:
        return [
            state for state in self._sessions.values()
            if not state.ended and not state.lease_fenced
        ]

    # ── lease узла поверх хранилища ──

    async def restore(self) -> int:
        """Поднять незавершённые занятия узла после перезапуска процесса."""
        if not self.store.persistent:
            return 0
        restored = await self.store.restore_active()
        adopted = 0
        for state in restored:
            adopted += await self._adopt(state)
        return adopted

    async def maintain_lease(self) -> None:
        """Один оборот супервизора: продлить свои lease, подхватить просроченные чужие.

        Непродлённая lease — не повод писать дальше «на авось»: любая ошибка
        продления закрывает занятие на узле (docs/arch/SCALE-OUT.md).
        """
        for state in self._live():
            try:
                await self.store.renew(state.session_id)
            except SessionLeaseLost:
                await self.fence(state)
            except Exception:  # неизвестное владение закрывается
                log.warning("продление lease занятия %s не удалось", state.session_id,
                            exc_info=True)
                await self.fence(state)
        try:
            claimed = await self.store.claim_expired()
        except Exception:  # следующий оборот попробует снова
            log.exception("не удалось подхватить занятия с истёкшей lease")
            return
        for state in claimed:
            current = self._sessions.get(state.session_id)
            if current is not None and not current.lease_fenced:
                continue
            await self._adopt(state)

    async def supervise_lease(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.maintain_lease()

    async def _adopt(self, state: SessionState) -> bool:
        self.stop_ticker(state.session_id)
        if state.socket_connected_at_checkpoint:
            # Разрыв произошёл при потере узла; старое время подключения не
            # доказывает, что курсант отсутствовал всё это время.
            state.socket_last_seen_at = now_utc()
            state.socket_connected_at_checkpoint = False
            try:
                await self.store.commit(state)
            except Exception:
                log.exception("не удалось сохранить присутствие после takeover %s", state.session_id)
                return False
        self.register(state)
        self.start_ticker(state.session_id)
        return True

    async def save_all(self) -> None:
        """Снимок живых занятий при остановке узла: следующий владелец продолжит с него."""
        if not self.store.persistent:
            return
        for state in self._live():
            try:
                await self.store.commit(state)
            except Exception:  # остановка должна освободить процесс
                log.exception("не удалось сохранить снимок %s при остановке", state.session_id)

    # ── операция: один commit, потом события ──

    @contextlib.asynccontextmanager
    async def operation(self, session_id: UUID) -> AsyncIterator[_Operation]:
        """Изменение занятия фиксируется одним `store.commit` на выходе.

        События копятся до коммита. Исключение внутри операции или сбой
        коммита отбрасывают их и закрывают занятие на узле (fail closed).
        """
        op = _Operation(session_id)
        token: Token = self._operation.set(op)
        try:
            yield op
            state = self._sessions.get(session_id)
            if state is not None and op.persist:
                if state.lease_fenced:
                    raise RuntimeError(LEASE_FENCED_MESSAGE)
                op.persist = False  # второй commit из обработчика отмены не нужен
                await asyncio.shield(self.store.commit(state, op.records))
        except asyncio.CancelledError:
            # Задачу сокета отменили посреди операции — это не сбой хранилища.
            # Сделанное фиксируется, события слать уже некому.
            op.events.clear()
            state = self._sessions.get(session_id)
            if state is not None and op.persist and not state.lease_fenced:
                try:
                    await asyncio.shield(self.store.commit(state, op.records))
                except Exception:
                    await self.fence(state)
            raise
        except Exception:
            op.events.clear()
            state = self._sessions.get(session_id)
            if state is not None and not state.lease_fenced:
                await self.fence(state)
            raise
        finally:
            op.open = False
            self._operation.reset(token)
        for registry, event in op.events:
            self._put(registry.get(session_id, set()), event)

    def record(self, session_id: UUID, record: Record) -> None:
        """Строка журнала уходит в commit текущей операции, не отдельной транзакцией."""
        op = self._operation.get()
        if op is None or not op.open or op.session_id != session_id:
            raise RuntimeError(f"запись занятия {session_id} вне операции")
        op.records.append(record)

    async def commit(self, session_id: UUID, *records: Record) -> None:
        """Операция из одних строк — реплика голосового контура."""
        async with self.operation(session_id):
            for record in records:
                self.record(session_id, record)

    def _send(self, registry: dict[UUID, set[asyncio.Queue]], session_id: UUID,
              event: BaseModel) -> None:
        op = self._operation.get()
        if (op is not None and op.open and op.session_id == session_id
                and not isinstance(event, ErrorEvent)):
            op.events.append((registry, event))
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

    async def _record_presence(self, session_id: UUID, *, station: bool) -> None:
        state = self.get(session_id)
        if state is None or state.ended or state.dds_phase != station:
            return
        async with self.operation(session_id):
            state.socket_last_seen_at = now_utc()
            present = self._trainee_stations if station else self._trainee_calls
            state.socket_connected_at_checkpoint = bool(present.get(session_id))

    @contextlib.asynccontextmanager
    async def trainee_socket(
        self, session_id: UUID, *, station: bool, trainee: bool,
    ) -> AsyncIterator[asyncio.Queue]:
        """Учесть только сокет курсанта; вещание преподавателю остаётся общим."""
        registry = self._stations if station else self._trainees
        presence = self._trainee_stations if station else self._trainee_calls
        with self._subscribe(registry, session_id) as queue:
            if trainee:
                presence.setdefault(session_id, set()).add(queue)
            try:
                if trainee:
                    try:
                        await self._record_presence(session_id, station=station)
                    except Exception:
                        if not self.is_lease_fenced(session_id):
                            raise
                yield queue
            finally:
                if trainee:
                    presence[session_id].discard(queue)
                    if not presence[session_id]:
                        presence.pop(session_id)
                    try:
                        await self._record_presence(session_id, station=station)
                    except Exception:
                        if not self.is_lease_fenced(session_id):
                            raise

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

    def station_connected(self, session_id: UUID) -> bool:
        return bool(self._trainee_stations.get(session_id))

    def trainee_connected(self, session_id: UUID) -> bool:
        return bool(self._trainee_calls.get(session_id))

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
        for state in list(self._sessions.values()):
            if state.voice is not None:
                await state.voice.close()

    async def _tick(self, session_id: UUID) -> None:
        try:
            while True:
                await asyncio.sleep(TICK_SECONDS)
                state = self.get(session_id)
                if state is None or state.ended:
                    return
                if state.dds_phase:
                    async with self.operation(session_id) as op:
                        active_before = state.desk.active_id
                        op.persist = bool(state.desk.deliver_due())
                        if state.desk.active_id != active_before and state.desk.active_id:
                            self.to_station(session_id, state.card_received_event())
                    # Keep the pending count and countdown live even while
                    # the active dispatcher card is being handled.
                    self.to_station(session_id, StationState(snapshot=state.station_snapshot()))
                tick = TimerTick(timers=state.shown_timers().snapshot())
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
