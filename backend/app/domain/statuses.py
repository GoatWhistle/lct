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
from uuid import UUID

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
# Заказчик уточнил, что диспетчер выбирает статусы и добавляет к ним свои
# комментарии. Требуем фиксировать источник/содержание сведений для каждой
# ручной отметки, а не обучать проставлению статусов без основания.
COMMENT_REQUIRED: frozenset[ServiceStatus] = frozenset({
    ServiceStatus.ACCEPTED,
    ServiceStatus.DECLINED,
    ServiceStatus.RESPONDING,
    ServiceStatus.ARRIVED,
    ServiceStatus.WORKING,
    ServiceStatus.COMPLETED,
    ServiceStatus.REFUSED,
})
REFUSAL_COMMENT_REQUIRED: frozenset[ServiceStatus] = frozenset({
    ServiceStatus.DECLINED,
    ServiceStatus.REFUSED,
})

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


#: Авторы отметок смежных служб — для журнала и разбора: преподаватель видит,
#: пришла отметка сценарием или её вызвал звонок курсанта. Метрики авторов
#: не различают: реакция после звонка — такая же реакция в карточке.
ADJACENT_AUTHOR = "смежная служба"
ADJACENT_CALL_AUTHOR = "смежная служба, по звонку диспетчера"

#: Смежная служба «реагирует»: успокаивать бригаду можно только при этих
#: статусах. Отказ — тоже отметка, но передать его как «служба будет» нельзя.
ADJACENT_REACTING: frozenset[ServiceStatus] = frozenset({
    ServiceStatus.ACCEPTED,
    ServiceStatus.RESPONDING,
    ServiceStatus.ARRIVED,
    ServiceStatus.WORKING,
    ServiceStatus.COMPLETED,
})


class PhoneReportRecord(BaseModel):
    service: str
    crew: str
    phase: str
    text: str
    at: datetime


class PhoneLineRecord(BaseModel):
    service: str
    crew: str
    speaker: str  # dispatcher | crew
    text: str
    at: datetime


class PhoneCallPending(BaseModel):
    service: str
    crew: str
    phase: str
    #: Вводная сценария: бригада звонит сама, а не отвечает на вызов диспетчера.
    incoming: bool = False
    #: Смежная служба, о которой спрашивает бригада во входящем вызове.
    adjacent_service: str | None = None


class DdsCardSummary(BaseModel):
    card_id: UUID
    scenario_id: str
    score_auto: float


class DdsQueueCard(BaseModel):
    """Строка общей очереди ДДС; таймер каждой карточки идёт независимо."""

    card_id: UUID
    scenario_id: str
    title: str
    address: str | None = None
    description: str | None = None
    incident_type: str | None = None
    victims_count: int | None = None
    received_at: datetime
    managed_service: str | None = None
    service_status: ServiceStatus
    card_status: CardStatus
    elapsed_ms: int
    limit_ms: int
    timer_stopped: bool
    active: bool


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
            f"«{SERVICE_STATUS_LABELS[status]}» требует комментария: укажите основание "
            "отметки и содержание полученных сведений"
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
        service for service, status in latest.items()
        if status in {ServiceStatus.ADDED, ServiceStatus.RECEIVED}
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
    recipient_services: list[str] = []
    managed_service: str | None = None
    statuses: dict[str, ServiceStatus]
    available: dict[str, list[ServiceStatus]]
    card: CardStatus
    log: list[StatusEntry]
    crew_options: list[str] = []
    crew_selected: str | None = None
    zone_decision: bool | None = None
    phone_reports: list[PhoneReportRecord] = []
    phone_lines: list[PhoneLineRecord] = []
    phone_pending: PhoneCallPending | None = None
    card_error_discovered: bool = False
    report_112_text: str | None = None
    card_id: UUID | None = None
    card_index: int = 1
    card_total: int = 1
    reply_text: str = ""
    completed_cards: list[DdsCardSummary] = []
    queue_cards: list[DdsQueueCard] = []
    pending_cards_count: int = 0
    next_arrival_in_seconds: int | None = None
    max_waiting_cards: int = 3
    #: Пульт на паузе: время не идёт, новая карточка не поступит (lct-39).
    paused: bool = False
