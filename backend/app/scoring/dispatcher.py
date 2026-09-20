"""Оценка работы диспетчера ДДС: статусы реагирования.

Вторая роль — второй набор ошибок. D1–D6 взяты из памятки заказчика, где они
перечислены поимённо и с реальными примерами (docs/spec/DATASET.md). Формулировки
не переписаны: преподаватель, который эту памятку читал, должен узнать их в разборе.

Детерминированно считаются D1, D2, D3, D4 и D6. D5 — полнота комментария — мягкий
критерий, его место у судьи (lct-13): «не принята: не обслуживаем» без указания,
куда передана информация, формально неотличимо от полного комментария.
"""

from app.domain.statuses import (
    COMMENT_REQUIRED,
    PRIMARY,
    SERVICE_STATUS_LABELS,
    ServiceStatus,
    StatusEntry,
    current,
)
from app.domain.taxonomy import Competency, ErrorCode, Finding, FindingSource

#: Статусы, означающие, что реагирование действительно шло.
PROGRESS = (ServiceStatus.RESPONDING, ServiceStatus.ARRIVED, ServiceStatus.WORKING)


def _finding(code: ErrorCode, summary: str, fact: str, norm: str | None = None) -> Finding:
    return Finding(
        code=code,
        source=FindingSource.DISPATCHER,
        summary=summary,
        fact=fact,
        norm=norm,
        ref="памятка «Работа на АРМ-112», раздел «Статусы реагирования»",
        competency=Competency.CARD,
    )


def evaluate_dispatcher(
    *,
    entries: list[StatusEntry],
    services: list[str],
    deadline_ms: int,
    elapsed_ms: int | None,
) -> list[Finding]:
    """Отметки по работе диспетчера. Пустой список — работа без нарушений.

    `services` — список оповещения карточки. Он же ответ на вопрос, профильно ли
    происшествие: служба в списке — значит происшествие её, и отказ «не наша
    компетенция» неправомерен.
    """
    findings: list[Finding] = []
    if not services:
        return findings

    for service in services:
        marks = [entry for entry in entries if entry.service == service]
        latest = current(entries, service)

        # D1 — первичного статуса нет вовсе.
        if not any(mark.status in PRIMARY for mark in marks):
            findings.append(
                _finding(
                    ErrorCode.D1,
                    f"{service}: статус реагирования не проставлен",
                    fact=(
                        f"прошло {elapsed_ms // 1000} с"
                        if elapsed_ms is not None
                        else "карточка осталась без отметки"
                    ),
                    norm=f"первичный статус в течение {deadline_ms // 1000} с",
                )
            )
            continue

        for mark in marks:
            # D4 — отказ без комментария.
            if mark.status in COMMENT_REQUIRED and not mark.comment.strip():
                findings.append(
                    _finding(
                        ErrorCode.D4,
                        f"{service}: «{SERVICE_STATUS_LABELS[mark.status]}» без комментария",
                        fact="причина отказа не указана",
                        norm="комментарий к отказу обязателен",
                    )
                )

        # D3 — отказ от профильного происшествия. Служба в списке оповещения
        # по классификатору, значит происшествие входит в её компетенцию.
        if latest is ServiceStatus.DECLINED:
            findings.append(
                _finding(
                    ErrorCode.D3,
                    f"{service}: отказ от происшествия, которое в её компетенции",
                    fact="служба есть в списке оповещения по ЕКП",
                    norm="отказываться от профильного происшествия нельзя",
                )
            )

        # D2 — «Принята», но работ не было и отказа тоже: статус не отражает факт.
        if latest is ServiceStatus.ACCEPTED:
            findings.append(
                _finding(
                    ErrorCode.D2,
                    f"{service}: «Принята», но о реагировании ничего не отмечено",
                    fact="после приёма статусов не было",
                    norm="статус должен соответствовать фактическому состоянию заявки",
                )
            )

        # D6 — работы завершены, а ход работ не отмечен. В памятке это отдельный
        # разбор: по такому происшествию идут повторные звонки, и другие службы
        # не видят, что реагирование вообще началось.
        if latest is ServiceStatus.COMPLETED and not any(
            mark.status in PROGRESS for mark in marks
        ):
            findings.append(
                _finding(
                    ErrorCode.D6,
                    f"{service}: работы завершены без отметок хода",
                    fact="начало реагирования и прибытие не отмечены",
                    norm="статусы хода работ проставляются по факту",
                )
            )

    return findings
