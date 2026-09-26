from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from scripts import backup_loop


def test_no_backup_is_due_immediately(monkeypatch):
    monkeypatch.setattr(backup_loop, "listing", lambda: [])
    assert backup_loop.seconds_until_due(datetime.now(timezone.utc), 86_400) == 0


def test_fresh_backup_waits_only_for_remaining_interval(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(backup_loop, "listing", lambda: [{"at": now - timedelta(hours=6)}])
    assert backup_loop.seconds_until_due(now, 86_400) == 18 * 3600


def test_overdue_backup_is_due_immediately(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(backup_loop, "listing", lambda: [{"at": now - timedelta(days=2)}])
    assert backup_loop.seconds_until_due(now, 86_400) == 0


def test_scheduler_waits_only_remainder_after_slow_backup(monkeypatch):
    settings = SimpleNamespace(
        backup_interval_seconds=86_400,
        backup_retry_seconds=300,
        backup_keep=14,
    )
    copies = []
    waits = []

    def listing():
        return copies

    def create():
        # Model pg_dump taking 20 minutes before finishing the scheduler cycle.
        copies.append({
            "name": "recent.sql",
            "at": datetime.now(timezone.utc) - timedelta(minutes=20),
        })
        return {"name": "recent.sql", "size_bytes": 123}

    def sleep(seconds):
        waits.append(seconds)
        raise RuntimeError("stop after observing next scheduled wait")

    monkeypatch.setattr(backup_loop, "get_settings", lambda: settings)
    monkeypatch.setattr(backup_loop, "listing", listing)
    monkeypatch.setattr(backup_loop, "create", create)
    monkeypatch.setattr(backup_loop, "prune", lambda keep: 0)
    monkeypatch.setattr(backup_loop.time, "sleep", sleep)

    try:
        backup_loop.run_forever()
    except RuntimeError as exc:
        assert str(exc) == "stop after observing next scheduled wait"

    assert len(waits) == 1
    assert 85_190 <= waits[0] <= 85_200
