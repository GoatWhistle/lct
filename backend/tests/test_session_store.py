"""Одна операция занятия — один commit хранилища, события только после него."""

import asyncio

import pytest

from app.domain.events import CallStarted, Speaker
from app.session.hub import LEASE_FENCED_MESSAGE, SessionHub
from app.session.state import now_utc
from app.session.store import MemorySessionStore, UtteranceAppended
from tests.test_session_checkpoint import dds_state


class FailingStore(MemorySessionStore):
    async def commit(self, state, records=()):
        raise OSError("simulated database partition")


def test_operation_commits_snapshot_and_records_once_then_publishes():
    store = MemorySessionStore()
    local_hub = SessionHub(store=store)
    state = local_hub.register(dds_state())
    sid = state.session_id

    with local_hub.trainee(sid) as trainee:
        async def operation():
            async with local_hub.operation(sid):
                entry = state.append(Speaker.OPERATOR, "Адрес?")
                local_hub.record(sid, UtteranceAppended(entry))
                local_hub.to_trainee(sid, CallStarted(started_at=now_utc()))
                assert trainee.empty(), "событие ушло до коммита"
                assert store.commits == []

        asyncio.run(operation())
        assert isinstance(trainee.get_nowait(), CallStarted)

    assert len(store.commits) == 1
    committed_id, records = store.commits[0]
    assert committed_id == sid
    assert [type(item) for item in records] == [UtteranceAppended]
    assert store.snapshot(sid)["transcript"][0]["text"] == "Адрес?"


def test_failed_commit_drops_events_and_fences_session():
    local_hub = SessionHub(store=FailingStore())
    state = local_hub.register(dds_state())
    sid = state.session_id

    with local_hub.observer(sid) as observers, \
         local_hub.trainee(sid) as trainee, \
         local_hub.station(sid) as station:
        async def operation():
            async with local_hub.operation(sid):
                local_hub.to_trainee(sid, CallStarted(started_at=now_utc()))

        with pytest.raises(OSError, match="partition"):
            asyncio.run(operation())

        assert state.lease_fenced
        assert local_hub.get(sid) is None
        for queue in (observers, trainee, station):
            assert queue.get_nowait().message == LEASE_FENCED_MESSAGE
            assert queue.empty(), "событие несостоявшейся операции утекло"


def test_record_outside_operation_is_rejected():
    local_hub = SessionHub(store=MemorySessionStore())
    state = local_hub.register(dds_state())
    with pytest.raises(RuntimeError):
        local_hub.record(state.session_id, UtteranceAppended(state.append(Speaker.OPERATOR, "x")))
