"""make backup: снять резервную копию базы.

Та же операция, что кнопка на АРМ администратора, — но вызываемая из cron:
ТЗ требует копирования не реже раза в сутки, а полагаться на то, что кто-то
нажмёт кнопку, нельзя.

    make backup                  снять копию
    0 3 * * * cd … && make backup   в crontab демо-машины
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.admin.backup import BackupError, create, listing  # noqa: E402

#: Сколько копий держать. Место на демо-машине не бесконечно, а копия за
#: позапрошлую неделю не нужна никому.
KEEP = max(1, int(os.environ.get("BACKUP_KEEP", "14")))


def main() -> int:
    try:
        created = create()
    except BackupError as exc:
        print(f"копия не снята: {exc}")
        return 1

    print(f"{created['name']}: {created['size_bytes'] // 1024} КБ")

    old = listing()[KEEP:]
    for item in old:
        (ROOT / "backups" / item["name"]).unlink(missing_ok=True)
    if old:
        print(f"удалено старых копий: {len(old)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
