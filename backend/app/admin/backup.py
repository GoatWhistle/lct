"""Резервные копии базы.

ТЗ требует копирования не реже раза в сутки. Копия — это `pg_dump`, и брать
его неоткуда, кроме самой базы: на машине с нативным бэкендом клиента Postgres
может не быть вовсе. Поэтому сначала пробуем локальный `pg_dump`, потом —
тот, что лежит в контейнере базы, и только если нет ни того ни другого,
честно говорим, чего не хватает.
"""

import base64
import logging
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from getpass import getuser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.config import get_settings

log = logging.getLogger(__name__)

DIR = Path(__file__).resolve().parents[2] / "backups"
#: Контейнер базы в docker compose проекта.
CONTAINER = os.environ.get("POSTGRES_CONTAINER", "lct-hack-postgres-1")
TIMEOUT_S = 120
# pg_dump 17 добавляет эту настройку в plain SQL, а целевая PostgreSQL 16 из
# Compose её ещё не знает. Остальной plain dump обратно совместим; удаляем
# только служебный SET, не данные и не DDL.
INCOMPATIBLE_SETTINGS = (b"SET transaction_timeout = 0;\n",)


class BackupError(RuntimeError):
    """Копию снять нечем или не удалось. Сообщение показывается администратору."""


def _restrict_file_permissions(fd: int, path: Path, platform_name: str | None = None) -> None:
    """Закрыть резервную копию от остальных локальных пользователей.

    chmod(0o600) не задаёт ACL на Windows, поэтому там icacls удаляет
    наследуемые разрешения и оставляет полный доступ владельцу процесса и
    системной учётной записи. При ошибке копию не публикуем.
    """
    platform_name = platform_name or os.name
    if platform_name == "nt":
        username = getuser()
        domain = os.environ.get("USERDOMAIN")
        if domain and "\\" not in username:
            username = f"{domain}\\{username}"
        system_root = Path(os.environ.get("SystemRoot", "C:\\Windows"))
        icacls = system_root / "System32" / "icacls.exe"
        try:
            result = subprocess.run(
                [str(icacls), str(path), "/inheritance:r", "/grant:r",
                 f"{username}:(F)", "*S-1-5-18:(F)"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupError("не удалось задать ACL резервной копии через icacls") from exc
        if result.returncode != 0:
            raise BackupError("Windows не применила закрытый ACL к резервной копии")
        return
    if hasattr(os, "fchmod"):
        try:
            os.fchmod(fd, 0o600)
        except OSError as exc:
            raise BackupError("не удалось ограничить права резервной копии") from exc
    else:
        try:
            os.chmod(path, 0o600)
        except OSError as exc:
            raise BackupError("не удалось ограничить права резервной копии") from exc


def _dsn() -> tuple[str, str, str, str, str]:
    """Разобрать DATABASE_URL на части для pg_dump."""
    url = get_settings().database_url
    try:
        parsed = urlsplit(url)
        user = unquote(parsed.username or "")
        password = unquote(parsed.password or "")
        host = parsed.hostname or ""
        port = parsed.port
        database = unquote(parsed.path.lstrip("/"))
    except ValueError as exc:
        # Не включать исходный URL в диагностику: он содержит пароль.
        raise BackupError("не удалось разобрать DATABASE_URL") from exc
    if not all((user, password, host, port, database)) or "/" in database:
        raise BackupError("не удалось разобрать DATABASE_URL")
    return user, password, host, str(port), database


def listing() -> list[dict]:
    """Готовые копии, новые первыми."""
    items = []
    try:
        with os.scandir(DIR) as entries:
            for entry in entries:
                if not entry.name.endswith(".sql") or not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                items.append({
                    "name": entry.name,
                    "size_bytes": stat.st_size,
                    "at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                })
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise BackupError(f"не удалось прочитать каталог резервных копий: {exc}") from exc
    return sorted(items, key=lambda item: item["at"], reverse=True)


def create() -> dict:
    """Снять копию. Возвращает описание файла, как в `listing()`."""
    user, password, host, port, database = _dsn()
    try:
        DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"недоступен каталог резервных копий: {exc}") from exc
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = DIR / f"lct-{stamp}.sql"

    if shutil.which("pg_dump"):
        command = ["pg_dump", "-h", host, "-p", port, "-U", user, "-d", database]
        env = {**os.environ, "PGPASSWORD": password}
        stdin_data = None
    elif shutil.which("docker"):
        # В контейнере база доступна как localhost, а не по хосту из DSN.
        # Пароль передаём через stdin, а не аргументом `docker exec -e`,
        # поскольку аргументы процесса могут быть видны другим пользователям.
        script = (
            "IFS= read -r encoded_password || exit 1; "
            'PGPASSWORD="$(printf %s "$encoded_password" | base64 -d)" || exit 1; '
            'export PGPASSWORD; exec pg_dump -U "$1" -d "$2"'
        )
        command = ["docker", "exec", "-i", CONTAINER, "sh", "-c", script,
                   "lct-pg-dump", user, database]
        env = dict(os.environ)
        stdin_data = base64.b64encode(password.encode("utf-8")) + b"\n"
    else:
        raise BackupError(
            "нечем снять копию: нет ни pg_dump, ни docker. "
            "Поставьте postgresql-client или запускайте базу в контейнере проекта"
        )

    try:
        result = subprocess.run(
            command, capture_output=True, timeout=TIMEOUT_S, env=env, input=stdin_data
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"pg_dump не уложился в {TIMEOUT_S} с") from exc
    except OSError as exc:
        raise BackupError(f"не запустился pg_dump: {exc}") from exc

    if result.returncode != 0:
        raise BackupError(result.stderr.decode(errors="replace")[:300] or "pg_dump завершился с ошибкой")

    dump = result.stdout
    for setting in INCOMPATIBLE_SETTINGS:
        dump = dump.replace(setting, b"")
    if not dump.startswith(b"--\n-- PostgreSQL database dump"):
        raise BackupError("pg_dump вернул неожиданный или пустой формат")
    # Публикуем только полностью записанный dump. При аварийном завершении
    # процесса список копий не должен показывать обрезанный файл как готовый.
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=DIR)
    except OSError as exc:
        raise BackupError(f"не удалось создать временный файл копии: {exc}") from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            _restrict_file_permissions(stream.fileno(), temporary)
            stream.write(dump)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            log.exception("не удалось удалить временную копию %s", temporary.name)
        if isinstance(exc, OSError):
            raise BackupError(f"не удалось записать резервную копию: {exc}") from exc
        raise
    size_bytes = len(dump)
    created_at = datetime.now(timezone.utc)
    log.info("резервная копия: %s, %d КБ", target.name, size_bytes // 1024)
    return {
        "name": target.name,
        "size_bytes": size_bytes,
        "at": created_at,
    }
