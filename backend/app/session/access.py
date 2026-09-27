"""Кто допущен к занятию — одно правило для сокетов и HTTP.

Живое состояние и строка сессии в БД несут одни и те же `owner_login` и
`trainee_id`, поэтому правило принимает любое из них. Владение группами и
сценариями — другое понятие и сюда не входит.
"""

from typing import Protocol
from uuid import UUID

from app.domain.roles import Role


class _Lesson(Protocol):
    owner_login: str | None
    trainee_id: UUID | None


def can_access(who, lesson: _Lesson) -> bool:
    """Преподаватель — только своё занятие, курсант — только назначенное ему.

    Занятие без курсанта не открывается ни одному курсанту: иначе `None == None`
    пустил бы учётку без профиля. Роль экрана проверяет вызывающий; администратору
    занятие не закрыто — ему доступна диагностика, а не оценки.
    """
    if who.role is Role.INSTRUCTOR:
        return lesson.owner_login == who.login
    if who.role is Role.TRAINEE:
        return lesson.trainee_id is not None and lesson.trainee_id == who.trainee_id
    return True
