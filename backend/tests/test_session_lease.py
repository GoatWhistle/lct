"""Lease занятий ведёт хаб поверх `SessionStore`, а не код запуска приложения."""

import asyncio
import re
from pathlib import Path

from app.session.hub import LEASE_FENCED_MESSAGE, SessionHub
from app.session.state import now_utc
from app.session.store import MemorySessionStore, SessionLeaseLost
from tests.test_session_checkpoint import dds_state

APP = Path(__file__).resolve().parents[1] / "app"


class LeaseStore(MemorySessionStore):
    persistent = True

    def __init__(self) -> None:
        super().__init__()
        self.renewed = []
        self.renew_errors: dict = {}
        self.expired = []
        self.active = []

    async def renew(self, session_id):
        self.renewed.append(session_id)
        error = self.renew_errors.get(session_id)
        if error is not None:
            raise error

    async def claim_expired(self, session_id=None):
        claimed, self.expired = self.expired, []
        return claimed

    async def restore_active(self):
        return list(self.active)


_hubs: list[SessionHub] = []


def make_hub(store) -> SessionHub:
    item = SessionHub(store=store)
    _hubs.append(item)
    return item


def run(coro):
    """Такты, запущенные подхватом, гасятся в том же цикле событий."""
    async def wrapper():
        try:
            return await coro
        finally:
            while _hubs:
                await _hubs.pop().shutdown()
    return asyncio.run(wrapper())


def test_lost_or_unconfirmed_lease_fences_only_that_session():
    store = LeaseStore()
    local_hub = make_hub(store)
    lost = local_hub.register(dds_state())
    broken = local_hub.register(dds_state())
    healthy = local_hub.register(dds_state())
    ended = local_hub.register(dds_state())
    ended.ended_at = now_utc()
    store.renew_errors = {
        lost.session_id: SessionLeaseLost("fenced"),
        broken.session_id: OSError("partition"),
    }

    with local_hub.trainee(lost.session_id) as trainee:
        run(local_hub.maintain_lease())
        assert trainee.get_nowait().message == LEASE_FENCED_MESSAGE

    assert lost.lease_fenced and broken.lease_fenced
    assert not healthy.lease_fenced
    assert ended.session_id not in store.renewed, "завершённое занятие lease не держит"


def test_expired_sessions_are_adopted_but_live_owner_is_not_replaced():
    store = LeaseStore()
    local_hub = make_hub(store)
    live = local_hub.register(dds_state())
    stale = local_hub.register(dds_state())
    stale.lease_fenced = True

    live_copy = live.model_copy()
    stale_copy = stale.model_copy()
    stale_copy.lease_fenced = False
    fresh = dds_state()
    store.expired = [live_copy, stale_copy, fresh]

    async def scenario():
        await local_hub.maintain_lease()
        return set(local_hub._tickers)

    tickers = run(scenario())
    assert local_hub.get(live.session_id) is live
    assert local_hub.get(stale.session_id) is stale_copy
    assert local_hub.get(fresh.session_id) is fresh
    assert {stale.session_id, fresh.session_id} <= tickers


def test_restore_registers_active_sessions_with_tickers():
    store = LeaseStore()
    store.active = [dds_state(), dds_state()]
    local_hub = make_hub(store)

    async def scenario():
        restored = await local_hub.restore()
        return restored, set(local_hub._tickers)

    restored, tickers = run(scenario())
    assert restored == 2
    assert {state.session_id for state in store.active} == tickers
    assert local_hub.live_count() == 2


def test_volatile_store_restores_nothing():
    local_hub = make_hub(MemorySessionStore())
    assert run(local_hub.restore()) == 0


def test_save_all_commits_only_live_sessions():
    store = LeaseStore()
    local_hub = make_hub(store)
    live = local_hub.register(dds_state())
    local_hub.register(dds_state()).ended_at = now_utc()
    local_hub.register(dds_state()).lease_fenced = True

    run(local_hub.save_all())
    assert [sid for sid, _records in store.commits] == [live.session_id]


def test_live_count_skips_ended_and_fenced():
    local_hub = make_hub(MemorySessionStore())
    local_hub.register(dds_state())
    local_hub.register(dds_state()).ended_at = now_utc()
    local_hub.register(dds_state()).lease_fenced = True
    assert local_hub.live_count() == 1


def test_application_code_does_not_touch_hub_internals():
    offenders = [
        f"{path.relative_to(APP)}:{number}"
        for path in APP.rglob("*.py")
        if path.name != "hub.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\bhub\._", line)
    ]
    assert offenders == []
