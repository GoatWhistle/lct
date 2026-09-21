"""Статусы реагирования служб и статусы карточки.

Взято из памятки ГБУ «Система 112» для дежурно-диспетчерских служб
(docs/spec/DATASET.md#статусы-реагирования). Это не наша модель работы, а её
модель: формулировки, порядок и обязательность комментариев — от заказчика,
и менять их по своему вкусу нельзя, иначе курсант выучит не то.

Главное свойство: статусы выбираются **последовательно**. Сначала доступны
только «Принята» и «Не принята»; всё остальное открывается после «Принята».
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ServiceStatus(StrEnum):
    """Статус реагирования одной службы из списка оповещения."""

    ADDED = "added"            # технический: карточка сохранена с оповещением
    RECEIVED = "received"      # технический: карточка открыта на АРМ
    ACCEPTED = "accepted"      # Принята
    DECLINED = "declined"      # Не принята
    RESPONDING = "responding"  # Начало реагирования
    ARRIVED = "arrived"        # Прибытие
    WORKING = "working"        # Проведение работ
    COMPLETED = "completed"    # Работы завершены
    REFUSED = "refused"        # Отказ от выполнения работ


SERVICE_STATUS_LABELS: dict[ServiceStatus, str] = {
    ServiceStatus.ADDED: "Добавлена",
    ServiceStatus.RECEIVED: "Получена службой",
    ServiceStatus.ACCEPTED: "Принята",
    ServiceStatus.DECLINED: "Не принята",
    ServiceStatus.RESPONDING: "Начало реагирования",
    ServiceStatus.ARRIVED: "Прибытие",
    ServiceStatus.WORKING: "Проведение работ",
    ServiceStatus.COMPLETED: "Работы завершены",
    ServiceStatus.REFUSED: "Отказ от выполнения работ",
}

#: Автомат переходов. «Не принята» ведёт только к «Принята» — служба может
#: передумать, но не может отказаться дважды по-разному.
NEXT: dict[ServiceStatus, tuple[ServiceStatus, ...]] = {
    ServiceStatus.ADDED: (ServiceStatus.ACCEPTED, ServiceStatus.DECLINED),
    ServiceStatus.RECEIVED: (ServiceStatus.ACCEPTED, ServiceStatus.DECLINED),
    ServiceStatus.DECLINED: (ServiceStatus.ACCEPTED,),
    ServiceStatus.ACCEPTED: (
        ServiceStatus.RESPONDING,
        ServiceStatus.ARRIVED,
        ServiceStatus.WORKING,
        ServiceStatus.COMPLETED,
        ServiceStatus.REFUSED,
    ),
    ServiceStatus.RESPONDING: (
        ServiceStatus.ARRIVED,
        ServiceStatus.WORKING,
        ServiceStatus.COMPLETED,
        ServiceStatus.REFUSED,
    ),
    ServiceStatus.ARRIVED: (
        ServiceStatus.WORKING,
        ServiceStatus.COMPLETED,
        ServiceStatus.REFUSED,
    ),
    ServiceStatus.WORKING: (ServiceStatus.COMPLETED, ServiceStatus.REFUSED),
    # Закрывают карточку от правки: дальше только звонок в отдел контроля.
    ServiceStatus.COMPLETED: (),
    ServiceStatus.REFUSED: (),
}

#: Без комментария не сохраняются. Это не валидация формы, а предмет обучения:
#: половина нарушений в памятке — отказ без указания, куда передана информация.
COMMENT_REQUIRED: frozenset[ServiceStatus] = frozenset(
    {ServiceStatus.DECLINED, ServiceStatus.REFUSED}
)

#: Первичные статусы: их ждут в норматив 30 секунд.
PRIMARY: frozenset[ServiceStatus] = frozenset(
    {ServiceStatus.ACCEPTED, ServiceStatus.DECLINED}
)

TERMINAL: frozenset[ServiceStatus] = frozenset(
    {ServiceStatus.COMPLETED, ServiceStatus.REFUSED}
)


class CardStatus(StrEnum):
    """Статус карточки целиком — считается из статусов служб, руками не ставится."""

    REGISTERED = "registered"        # Зарегистрирована
    PROCESSED = "processed"          # Отработана: оповещение завершено
    CHECKED = "checked"              # Проверена главным специалистом
    NOT_NOTIFIED = "not_notified"    # Не оповещено: служба промолчала в норматив
    REFUSED = "refused"              # Отказ
    NOT_COMPLETED = "not_completed"  # Не завершено: 48 часов без завершения работ
    COMPLETED = "completed"          # Завершена


CARD_STATUS_LABELS: dict[CardStatus, str] = {
    CardStatus.REGISTERED: "Зарегистрирована",
    CardStatus.PROCESSED: "Отработана",
    CardStatus.CHECKED: "Проверена",
    CardStatus.NOT_NOTIFIED: "Не оповещено",
    CardStatus.REFUSED: "Отказ",
    CardStatus.NOT_COMPLETED: "Не завершено",
    CardStatus.COMPLETED: "Завершена",
}

#: Красные в интерфейсе и в разборе: именно они попадают в раздел отдела
#: контроля реагирования. Цвет несёт смысл только здесь (docs/arch/FRONTEND.md).
ALARMING: frozenset[CardStatus] = frozenset(
    {CardStatus.NOT_NOTIFIED, CardStatus.REFUSED, CardStatus.NOT_COMPLETED}
)


class StatusEntry(BaseModel):
    """Одна отметка службы. Комментарий — часть отметки, а не примечание к ней."""

    service: str
    status: ServiceStatus
    at: datetime
    comment: str = ""
    author: str = ""


class StatusError(ValueError):
    """Переход запрещён автоматом или нет обязательного комментария."""


def current(entries: list[StatusEntry], service: str) -> ServiceStatus:
    """Последний статус службы. Без отметок — «Добавлена»."""
    for entry in reversed(entries):
        if entry.service == service:
            return entry.status
    return ServiceStatus.ADDED


def check(entries: list[StatusEntry], service: str, status: ServiceStatus, comment: str) -> None:
    """Проверить отметку до записи. Бросает `StatusError` с текстом для курсанта."""
    now = current(entries, service)
    if status not in NEXT[now]:
        allowed = ", ".join(SERVICE_STATUS_LABELS[item] for item in NEXT[now]) or "ничего"
        raise StatusError(
            f"после «{SERVICE_STATUS_LABELS[now]}» доступно: {allowed}"
            + ("" if NEXT[now] else " — карточка закрыта для редактирования")
        )
    if status in COMMENT_REQUIRED and not comment.strip():
        raise StatusError(
            f"«{SERVICE_STATUS_LABELS[status]}» требует комментария: причина и то, "
            "куда передана информация"
        )


def card_status(
    entries: list[StatusEntry], services: list[str], notify_deadline_passed: bool = False
) -> CardStatus:
    """Статус карточки по статусам служб.

    Упрощение против боевой системы, и оно намеренное: статусы «Проверена»
    и «Не завершено» требуют главного специалиста и 48 часов — ни того, ни
    другого в занятии на полтора часа нет. Остальные четыре считаются честно.
    """
    if not services:
        return CardStatus.COMPLETED

    latest = {service: current(entries, service) for service in services}
    silent = [
        service for service, status in latest.items() if status not in PRIMARY | TERMINAL
    ]
    if silent and notify_deadline_passed:
        return CardStatus.NOT_NOTIFIED
    if any(
        status in {ServiceStatus.DECLINED, ServiceStatus.REFUSED} for status in latest.values()
    ):
        return CardStatus.REFUSED
    if all(status is ServiceStatus.COMPLETED for status in latest.values()):
        return CardStatus.COMPLETED
    if silent:
        return CardStatus.REGISTERED
    return CardStatus.PROCESSED


class StationSnapshot(BaseModel):
    """Что видит диспетчер: службы, их статусы и что доступно дальше.

    Все поля обязательные, хотя списки и могут быть пустыми: снимок всегда
    собирается целиком, а необязательное поле уезжает в TypeScript как
    `| undefined` и заставляет фронт проверять то, чего не бывает.
    """

    services: list[str]
    statuses: dict[str, ServiceStatus]
    available: dict[str, list[ServiceStatus]]
    card: CardStatus
    log: list[StatusEntry]
