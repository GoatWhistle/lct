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
from app.domain.events import Metric
from app.domain.timers import GOST_REF

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


def dispatcher_metrics(state, deadline_ms: int) -> list[Metric]:
    """Числовая часть оценки ДДС; каждый проверяемый шаг имеет факт и норму.

    Веса предварительные — до утверждения методистом. Неподтверждённые
    телефонные действия нельзя засчитывать по одной кнопке статуса.
    """
    metrics: list[Metric] = []
    for service in state.notified_services():
        marks = [mark for mark in state.status_log if mark.service == service]
        statuses = {mark.status for mark in marks}
        reports = {report.phase for report in state.phone_reports if report.service == service}
        selected_crew = state.crew_assignments.get(service)
        selected = selected_crew is not None

        def add(key: str, title: str, passed: bool, fact: str, norm: str, weight: float = 1.0):
            metrics.append(Metric(
                key=key, title=f"{service}: {title}", fact=fact, norm=norm,
                ref=GOST_REF if key == "dds_ack" else "памятка «Работа на АРМ-112», статусы реагирования",
                passed=passed, weight=weight,
            ))

        primary = next((mark for mark in marks if mark.status in PRIMARY), None)
        elapsed = (int((primary.at - state.dispatched_at).total_seconds() * 1000)
                   if primary and state.dispatched_at else None)
        add("dds_primary", "решение по карточке", primary is not None,
            primary.status.value if primary else "решение отсутствует", "Принята или Не принята", 2.0)
        add("dds_ack", "первичная отметка за 30 с",
            elapsed is not None and elapsed <= deadline_ms,
            f"{elapsed / 1000:.1f} с" if elapsed is not None else "не отмечено",
            f"≤ {deadline_ms // 1000} с")
        if primary is None:
            continue
        add("dds_decision", "профильное реагирование",
            primary.status is ServiceStatus.ACCEPTED,
            primary.status.value, "профильную заявку принять", 2.0)
        if primary.status is ServiceStatus.DECLINED:
            continue
        add("dds_crew", "ручной выбор бригады", selected,
            selected_crew or "бригада не выбрана", "выбрать конкретную бригаду", 1.5)
        add("dds_contact", "связь с бригадой", bool(reports),
            ", ".join(sorted(reports)) or "докладов нет", "получить доклад о выезде", 1.5)
        expected = {
            "dispatched": ServiceStatus.RESPONDING,
            "arrived": ServiceStatus.ARRIVED,
            "working": ServiceStatus.WORKING,
        }
        aligned = all(status in statuses for phase, status in expected.items() if phase in reports)
        all_progress = all(phase in reports and status in statuses for phase, status in expected.items())
        add("dds_progress", "статусы по докладам", aligned and all_progress,
            ", ".join(status.value for status in statuses) or "статусов нет",
            "выезд, прибытие и работы отмечены после докладов", 2.0)
        add("dds_completion", "закрытие работ по докладу",
            "completed" in reports and ServiceStatus.COMPLETED in statuses,
            "завершено" if ServiceStatus.COMPLETED in statuses else "не завершено",
            "доклад о завершении и статус «Работы завершены»")
    return metrics
