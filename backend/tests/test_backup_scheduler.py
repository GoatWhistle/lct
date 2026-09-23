from datetime import datetime, timedelta, timezone

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
