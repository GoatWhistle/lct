"""Вход в сокет занятия и выполнение команды: права и fencing одного модуля."""

import asyncio
import json
from uuid import uuid4

import pytest

from app.api.auth import Principal
from app.api.ws import call as call_ws
from app.api.ws import control as control_ws
from app.api.ws import session as session_ws
from app.api.ws import station as station_ws
from app.domain.events import CommandAck, Exercise, SessionMode, StationState
from app.domain.roles import Role
from app.session.access import can_access
from app.session.hub import LEASE_FENCED_MESSAGE, hub
from app.session.state import SessionState
from app.session.store import MemorySessionStore

TEACHER = Principal(login="teacher-a", full_name="Преподаватель A", role=Role.INSTRUCTOR)
STRANGER = Principal(login="teacher-b", full_name="Преподаватель B", role=Role.INSTRUCTOR)
ADMIN = Principal(login="root", full_name="Администратор", role=Role.ADMIN)


def trainee(trainee_id=None) -> Principal:
    return Principal(login="cadet", full_name="Курсант", role=Role.TRAINEE,
                     trainee_id=trainee_id or uuid4())


def lesson_state(**fields) -> SessionState:
    return SessionState(
        session_id=uuid4(), scenario_id="test", scenario_title="Тест", level="L1",
        mode=SessionMode.TRAINING, owner_login=TEACHER.login, **fields,
    )


class Socket:
    """Сокет без сети: что отправлено и с каким кодом закрыт."""

    def __init__(self, incoming=()):
        self.accepted = False
        self.closed = False
        self.close_code = None
        self.sent: list[dict] = []
        self.incoming = list(incoming)

    async def accept(self):
        self.accepted = True

    async def send_text(self, message):
        self.sent.append(json.loads(message))

    async def send_bytes(self, _data):
        pass

    async def close(self, code=None):
        self.closed = True
        self.close_code = code

    async def receive_json(self):
        if self.incoming:
            return self.incoming.pop(0)
        await asyncio.sleep(5)

    async def receive(self):
        if self.incoming:
            return {"type": "websocket.receive", "text": json.dumps(self.incoming.pop(0))}
        await asyncio.sleep(5)
        return {"type": "websocket.disconnect"}


class BrokenStore(MemorySessionStore):
    async def commit(self, _state, _records=()):
        raise OSError("simulated database partition")


@pytest.fixture
def lesson(monkeypatch):
    """Занятие в общем хабе и принципал, которого подставляет тест."""
    monkeypatch.setattr(session_ws, "websocket_origin_allowed", lambda _ws: True)
    current = {"who": TEACHER}
    monkeypatch.setattr(session_ws, "principal_of", lambda _ws: current["who"])
    old_store = hub.store
    hub.store = MemorySessionStore()
    states = []

    def make(who=TEACHER, **fields):
        current["who"] = who
        state = lesson_state(**fields)
        hub.register(state)
        states.append(state)
        return state

    make.set_who = lambda who: current.update(who=who)
    yield make
    hub.store = old_store
    for state in states:
        hub.drop(state.session_id)


def enter(socket, session_id, roles=(Role.INSTRUCTOR, Role.TRAINEE), **options):
    return asyncio.run(session_ws.session_socket(socket, session_id, roles, **options))


# ── предикат доступа ──


def test_access_is_owner_instructor_or_assigned_trainee():
    cadet = trainee()
    state = lesson_state(trainee_id=cadet.trainee_id)
    assert can_access(TEACHER, state)
    assert not can_access(STRANGER, state)
    assert can_access(cadet, state)
    assert not can_access(trainee(), state)
    assert can_access(ADMIN, state), "администратор видит занятие для диагностики"


def test_unassigned_session_is_closed_for_every_trainee():
    assert not can_access(Principal(login="x", full_name="x", role=Role.TRAINEE), lesson_state())


# ── вход в сокет ──


def test_foreign_origin_is_closed_with_1008_before_accept(lesson, monkeypatch):
    state = lesson()
    monkeypatch.setattr(session_ws, "websocket_origin_allowed", lambda _ws: False)
    socket = Socket()
    assert enter(socket, state.session_id) is None
    assert not socket.accepted and socket.close_code == 1008


def test_fenced_session_is_closed_with_1012_before_accept(lesson):
    state = lesson()
    state.lease_fenced = True
    socket = Socket()
    assert enter(socket, state.session_id) is None
    assert not socket.accepted and socket.close_code == 1012


@pytest.mark.parametrize("who", [None, ADMIN])
def test_missing_principal_or_wrong_role_is_forbidden(lesson, who):
    state = lesson()
    lesson.set_who(who)
    socket = Socket()
    assert enter(socket, state.session_id) is None
    assert socket.accepted and socket.closed
    assert [event["code"] for event in socket.sent] == ["forbidden"]


def test_missing_session_is_not_found(lesson):
    lesson()
    socket = Socket()
    assert enter(socket, uuid4()) is None
    assert socket.closed
    assert [event["code"] for event in socket.sent] == ["session_not_found"]


def test_foreign_instructor_sees_not_found_without_owner_login(lesson):
    state = lesson()
    lesson.set_who(STRANGER)
    socket = Socket()
    assert enter(socket, state.session_id) is None
    assert socket.closed
    assert [event["code"] for event in socket.sent] == ["session_not_found"]
    assert TEACHER.login not in json.dumps(socket.sent, ensure_ascii=False)


@pytest.mark.parametrize("assigned", [True, False])
def test_unassigned_trainee_is_forbidden(lesson, assigned):
    state = lesson(trainee_id=uuid4() if assigned else None)
    lesson.set_who(trainee())
    socket = Socket()
    assert enter(socket, state.session_id) is None
    assert socket.closed
    assert [event["code"] for event in socket.sent] == ["forbidden"]


def test_owner_and_assigned_trainee_enter(lesson):
    cadet = trainee()
    state = lesson(trainee_id=cadet.trainee_id)
    for who in (TEACHER, cadet):
        lesson.set_who(who)
        socket = Socket()
        assert enter(socket, state.session_id) == (who, state)
        assert socket.accepted and not socket.closed and socket.sent == []


def test_optional_session_lets_control_in_before_start(lesson):
    lesson()
    socket = Socket()
    session_id = uuid4()
    assert enter(socket, session_id, (Role.INSTRUCTOR,), require_state=False) == (TEACHER, None)
    assert socket.accepted and not socket.closed


# ── выполнение команды ──


def test_repeated_command_is_acked_without_second_transition(lesson):
    state = lesson()
    command_id = uuid4()
    calls = []

    async def scenario():
        with hub.station(state.session_id) as queue:
            async def handler():
                calls.append(1)
            for _ in range(2):
                assert await session_ws.run_command(
                    Socket(), state.session_id, handler, command_id=command_id,
                    ack=lambda event: hub.to_station(state.session_id, event),
                )
            return [queue.get_nowait() for _ in range(queue.qsize())]

    events = asyncio.run(scenario())
    assert calls == [1]
    assert events == [CommandAck(command_id=command_id)] * 2
    assert str(command_id) in state.processed_station_commands


def test_ack_is_sent_only_after_commit(lesson):
    state = lesson()
    command_id = uuid4()
    seen_at_commit = []

    class WatchingStore(MemorySessionStore):
        queue: asyncio.Queue

        async def commit(self, state, records=()):
            seen_at_commit.append(
                (self.queue.qsize(), str(command_id) in state.processed_station_commands)
            )

    watching = hub.store = WatchingStore()

    async def handler():
        hub.to_station(state.session_id, StationState(snapshot=state.station_snapshot()))

    async def scenario():
        with hub.station(state.session_id) as queue:
            watching.queue = queue
            await session_ws.run_command(
                Socket(), state.session_id, handler, command_id=command_id,
                ack=lambda event: hub.to_station(state.session_id, event),
            )
            return [queue.get_nowait() for _ in range(queue.qsize())]

    events = asyncio.run(scenario())
    assert seen_at_commit == [(0, True)], "до commit клиенту ничего не ушло, id уже в снимке"
    assert events[-1] == CommandAck(command_id=command_id)


def test_fencing_without_queue_is_reported_on_the_socket(lesson):
    state = lesson()
    hub.store = BrokenStore()
    socket = Socket()

    async def handler():
        pass

    assert not asyncio.run(session_ws.run_command(socket, state.session_id, handler))
    assert state.lease_fenced
    assert socket.sent[-1] == {"type": "error", "code": "internal", "message": LEASE_FENCED_MESSAGE}
    assert socket.close_code == 1012


def test_error_without_fencing_propagates():
    """Занятия нет на узле — fencing не было, исключение уходит как раньше."""
    async def handler():
        raise ValueError("bug")

    with pytest.raises(ValueError):
        asyncio.run(session_ws.run_command(Socket(), uuid4(), handler))


# ── fencing на каналах: ErrorEvent и 1012, а не 1006 ──


def _fenced_close(socket):
    assert socket.close_code == 1012
    fenced = [event for event in socket.sent if event.get("type") == "error"]
    assert fenced[-1] == {"type": "error", "code": "internal", "message": LEASE_FENCED_MESSAGE}


def test_station_command_fencing_closes_with_1012(lesson):
    state = lesson(exercise=Exercise.DDS)
    hub.store = BrokenStore()
    socket = Socket([{"type": "card.ack", "comment": "принято",
                     "_command_id": str(uuid4())}])
    asyncio.run(asyncio.wait_for(station_ws.station(socket, state.session_id), 3))
    assert state.lease_fenced
    _fenced_close(socket)


def test_call_command_fencing_closes_with_1012(lesson):
    state = lesson(exercise=Exercise.CALL)
    hub.store = BrokenStore()
    socket = Socket([{"type": "callback.dial"}])
    asyncio.run(asyncio.wait_for(call_ws.call(socket, state.session_id), 3))
    assert state.lease_fenced
    _fenced_close(socket)


def test_control_command_fencing_closes_with_1012(lesson):
    state = lesson()
    hub.store = BrokenStore()
    socket = Socket([{"type": "reference.play"}])
    asyncio.run(asyncio.wait_for(control_ws.control(socket, state.session_id), 3))
    assert state.lease_fenced
    _fenced_close(socket)
