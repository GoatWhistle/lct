"""Резервные копии базы.

ТЗ требует копирования не реже раза в сутки. Копия — это `pg_dump`, и брать
его неоткуда, кроме самой базы: на машине с нативным бэкендом клиента Postgres
может не быть вовсе. Поэтому сначала пробуем локальный `pg_dump`, потом —
тот, что лежит в контейнере базы, и только если нет ни того ни другого,
честно говорим, чего не хватает.
"""

import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

DIR = Path(__file__).resolve().parents[2] / "backups"
#: Контейнер базы в docker compose проекта.
CONTAINER = os.environ.get("POSTGRES_CONTAINER", "lct-hack-postgres-1")
TIMEOUT_S = 120


class BackupError(RuntimeError):
    """Копию снять нечем или не удалось. Сообщение показывается администратору."""


def _dsn() -> tuple[str, str, str, str, str]:
    """Разобрать DATABASE_URL на части для pg_dump."""
    url = get_settings().database_url
    match = re.match(r".*://([^:]+):([^@]+)@([^:/]+):(\d+)/(\w+)", url)
    if not match:
        raise BackupError(f"не разобрать DATABASE_URL: {url}")
    return match.groups()  # user, password, host, port, database


def listing() -> list[dict]:
    """Готовые копии, новые первыми."""
    if not DIR.exists():
        return []
    items = [
        {
            "name": path.name,
            "size_bytes": path.stat().st_size,
            "at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
        }
        for path in DIR.glob("*.sql")
    ]
    return sorted(items, key=lambda item: item["at"], reverse=True)


def create() -> dict:
    """Снять копию. Возвращает описание файла, как в `listing()`."""
    user, password, host, port, database = _dsn()
    DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = DIR / f"lct-{stamp}.sql"

    if shutil.which("pg_dump"):
        command = ["pg_dump", "-h", host, "-p", port, "-U", user, "-d", database]
        env = {**os.environ, "PGPASSWORD": password}
    elif shutil.which("docker"):
        # В контейнере база доступна как localhost, а не по хосту из DSN.
        command = ["docker", "exec", "-e", f"PGPASSWORD={password}", CONTAINER,
                   "pg_dump", "-U", user, "-d", database]
        env = dict(os.environ)
    else:
        raise BackupError(
            "нечем снять копию: нет ни pg_dump, ни docker. "
            "Поставьте postgresql-client или запускайте базу в контейнере проекта"
        )

    try:
        result = subprocess.run(command, capture_output=True, timeout=TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"pg_dump не уложился в {TIMEOUT_S} с") from exc
    except OSError as exc:
        raise BackupError(f"не запустился pg_dump: {exc}") from exc

    if result.returncode != 0:
        raise BackupError(result.stderr.decode(errors="replace")[:300] or "pg_dump завершился с ошибкой")

    target.write_bytes(result.stdout)
    log.info("резервная копия: %s, %d КБ", target.name, target.stat().st_size // 1024)
    return {
        "name": target.name,
        "size_bytes": target.stat().st_size,
        "at": datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc),
    }
