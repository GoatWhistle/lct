"""make users: завести учётные записи для занятия.

Без входа система не работает (lct-23), а заводить первого администратора
руками в базе — плохой первый шаг для того, кто разворачивает стенд.
Скрипт создаёт по одной записи на роль и печатает пароли один раз.

Пароли случайные: зашитый в репозиторий «admin/admin» переживёт сдачу
и уедет в реальный класс.

    make users            создать недостающие
    make users force=1    пересоздать пароли существующих
"""

import asyncio
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.api.auth import hash_password  # noqa: E402
from app.db.base import get_sessionmaker  # noqa: E402
from app.db.models import Trainee, User  # noqa: E402
from app.domain.roles import Role  # noqa: E402

PEOPLE = [
    ("admin", "Администратор стенда", Role.ADMIN, None),
    ("teacher", "Преподаватель", Role.INSTRUCTOR, None),
    ("trainee", "Курсант", Role.TRAINEE, "Служба 101"),
]


async def main() -> int:
    force = bool(os.environ.get("force"))
    created: list[tuple[str, str]] = []

    async with get_sessionmaker()() as db:
        for login, full_name, role, service in PEOPLE:
            user = await db.scalar(select(User).where(User.login == login))
            if user is not None and not force:
                print(f"  {login}: уже есть")
                continue

            password = secrets.token_urlsafe(9)
            if user is None:
                user = User(login=login, full_name=full_name, role=role.value, service=service)
                db.add(user)
            user.password_hash = hash_password(password)
            user.blocked = False

            # У обучающегося должна быть карточка курсанта: по ней собираются
            # профиль и история, и по ней же разбор закрывается от чужих.
            if role is Role.TRAINEE and user.trainee_id is None:
                trainee = Trainee(name=full_name)
                db.add(trainee)
                await db.flush()
                user.trainee_id = trainee.id

            created.append((login, password))
        await db.commit()

    if not created:
        print("все учётные записи на месте (пересоздать: make users force=1)")
        return 0

    print("\nЗаписано. Пароли показываются один раз:\n")
    for login, password in created:
        print(f"  {login:10} {password}")
    print("\nСменить пароль можно только пересозданием: экран смены пароля — lct-24.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
