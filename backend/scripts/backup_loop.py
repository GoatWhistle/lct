"""Автоматическая резервная копия PostgreSQL не реже раза в сутки.

Процесс живёт отдельным Compose-сервисом. Он делает копию сразу, если свежей
нет, затем ориентируется на фактическое время последнего успешного файла.
После ошибки повторяет попытку чаще, но не удаляет существующие копии.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.admin.backup import BackupError, DIR, create, listing  # noqa: E402
from app.config import get_settings  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s backup-scheduler %(message)s",
)
log = logging.getLogger("backup-scheduler")


def seconds_until_due(now: datetime, interval: int) -> float:
    copies = listing()
    if not copies:
        return 0.0
    age = (now - copies[0]["at"]).total_seconds()
    return max(0.0, interval - age)


def prune(keep: int) -> int:
    old = listing()[keep:]
    deleted = 0
    for item in old:
        try:
            (DIR / item["name"]).unlink(missing_ok=True)
            deleted += 1
        except OSError as exc:
            # A locked/readonly old file must not take down the daily scheduler.
            log.error("не удалось удалить старую копию %s: %s", item["name"], exc)
    return deleted


def run_forever() -> None:
    settings = get_settings()
    interval = max(60, settings.backup_interval_seconds)
    retry = max(10, min(settings.backup_retry_seconds, interval))
    keep = max(1, settings.backup_keep)
    log.info("запущен: интервал=%s с, хранить=%s", interval, keep)

    while True:
        try:
            wait = seconds_until_due(datetime.now(timezone.utc), interval)
        except BackupError as exc:
            log.error("не удалось проверить резервные копии: %s; повтор через %s с", exc, retry)
            time.sleep(retry)
            continue
        if wait > 0:
            time.sleep(wait)
            continue
        try:
            made = create()
            deleted = prune(keep)
            log.info("готово: %s (%s байт), удалено старых=%s",
                     made["name"], made["size_bytes"], deleted)
        except BackupError as exc:
            log.error("цикл резервного копирования не завершён: %s; повтор через %s с", exc, retry)
            time.sleep(retry)
            continue
        # Re-evaluate the age of the completed copy at the top of the loop.
        # Sleeping a full interval here would make the real gap
        # (dump duration + interval) and could exceed the 24-hour requirement.


if __name__ == "__main__":
    run_forever()
