"""Роли пользователей. Три названы в ТЗ поимённо, четвёртой нет.

Роль лежит в домене, а не в модуле входа: по ней разводятся и экраны фронта,
и каналы сокетов, и она уезжает в TypeScript через `make types`.
"""

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    INSTRUCTOR = "instructor"
    TRAINEE = "trainee"


ROLE_LABELS: dict[Role, str] = {
    Role.ADMIN: "Администратор системы",
    Role.INSTRUCTOR: "Преподаватель",
    Role.TRAINEE: "Обучающийся",
}

#: Что кому доступно. Ограничения из ТЗ: преподаватель не меняет системные
#: настройки, администратор не вмешивается в оценки, обучающийся не видит
#: чужих результатов.
SCREENS: dict[Role, tuple[str, ...]] = {
    Role.ADMIN: ("/admin", "/profile"),
    Role.INSTRUCTOR: ("/instructor", "/wall", "/profile", "/dds"),
    Role.TRAINEE: ("/trainee", "/profile"),
}
