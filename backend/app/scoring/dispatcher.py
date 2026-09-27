"""Оценка работы диспетчера ДДС: статусы реагирования.

Вторая роль — второй набор ошибок. D1–D6 взяты из памятки заказчика, где они
перечислены поимённо и с реальными примерами (docs/spec/DATASET.md). Формулировки
не переписаны: преподаватель, который эту памятку читал, должен узнать их в разборе.

Детерминированно считаются D1–D6. D5 проверяет структуру отдельного доклада и
получателя сведений; пропуск по каждому комментарию виден отдельно. К каждой ручной
отметке обязательны непустые поля «Основание» и «Сведения»; их наличие входит в
числовую метрику `dds_reply`, но не подтверждает истинность текста. Это не
привязывает статус к звонку или SIP: сведения можно получить по любому рабочему каналу.
"""

from app.domain.statuses import (
    COMMENT_REQUIRED,
    PhoneReportRecord,
    PRIMARY,
    REFUSAL_COMMENT_REQUIRED,
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
REPORT_STATUS = {
    "dispatched": ServiceStatus.RESPONDING,
    "arrived": ServiceStatus.ARRIVED,
    "working": ServiceStatus.WORKING,
    "completed": ServiceStatus.COMPLETED,
}


def _report_reactions(
    entries: list[StatusEntry], reports: list[PhoneReportRecord], service: str,
    limit_seconds: int,
) -> tuple[list[Metric], list[Finding]]:
    """Сверить каждый доклад с отметкой той же службы и фазы."""
    metrics: list[Metric] = []
    findings: list[Finding] = []
    for report in reports:
        if report.service != service or report.phase not in REPORT_STATUS:
            continue
        status = REPORT_STATUS[report.phase]
        label = SERVICE_STATUS_LABELS[status]
        mark = next((item for item in entries if item.service == service and item.status == status), None)
        elapsed = max(0.0, (mark.at - report.at).total_seconds()) if mark else None
        passed = elapsed is not None and elapsed <= limit_seconds
        if mark is None:
            fact = f"после доклада бригады «{report.crew}» от {report.at.isoformat()} статус «{label}» не отмечен"
        elif mark.at <= report.at:
            fact = f"статус «{label}» отмечен до доклада бригады «{report.crew}» от {report.at.isoformat()}"
        else:
            fact = (f"статус «{label}» отмечен через {elapsed:.1f} с после доклада "
                    f"бригады «{report.crew}» от {report.at.isoformat()}")
        norm = f"статус «{label}» не позже {limit_seconds} с после доклада"
        metrics.append(Metric(
            key="dds_report_reaction", title=f"{service}: реакция на доклад — {label}",
            fact=fact, norm=norm, ref="памятка «Работа на АРМ-112», статусы реагирования",
            passed=passed, weight=1.0,
        ))
        if not passed:
            findings.append(_finding(
                ErrorCode.D6, (f"{service}: статус хода работ не отмечен после доклада"
                              if mark is None else
                              f"{service}: задержана отметка хода работ после доклада"),
                fact=fact, norm=norm,
            ))
    return metrics, findings


def _finding(code: ErrorCode, summary: str, fact: str, norm: str | None = None) -> Finding:
    return Finding(
        code=code,
        source=FindingSource.DISPATCHER,
        summary=summary,
        fact=fact,
        norm=norm,
        ref="памятка «Работа на АРМ-112», раздел «Статусы реагирования»",
        competency=(
            Competency.COMMUNICATION if code is ErrorCode.D5 else Competency.CARD
        ),
    )


_RECIPIENT_TERMS = (
    "бригад",
    "старш",
    "дежурн",
    "диспетчер",
    "оператор",
    "заявител",
    "пострадавш",
    "мвд",
    "полици",
    "мчс",
    "скорая",
    "медицинск",
    "аварийн",
    "служба 101",
    "служба 102",
    "служба 103",
    "служба 104",
)


def _has_recipient(text: str, service: str, crew: str | None) -> bool:
    value = text.casefold()
    if any(term in value for term in _RECIPIENT_TERMS):
        return True
    candidates = [service, crew or ""]
    return any(len(item.strip()) >= 3 and item.strip().casefold() in value for item in candidates)


def _has_basis_and_information(text: str) -> bool:
    """Require two explicit fields; this checks structure, not factual truth."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().casefold() in {"основание", "сведения"}:
            fields[label.strip().casefold()] = value.strip()
    return bool(fields.get("основание") and fields.get("сведения"))


def evaluate_dispatcher(
    *,
    entries: list[StatusEntry],
    services: list[str],
    crew_assignments: dict[str, str] | None = None,
    deadline_ms: int,
    elapsed_ms: int | None,
    reply_text: str = "",
    expected_decision: str = "accept",
    expected_decision_reason: str | None = None,
    phone_reports: list[PhoneReportRecord] | None = None,
    report_reaction_limit_seconds: int = 45,
) -> list[Finding]:
    """Отметки по работе диспетчера. Пустой список — работа без нарушений.

    `services` — список оповещения карточки. Он же ответ на вопрос, профильно ли
    происшествие: служба в списке — значит происшествие её, и отказ «не наша
    компетенция» неправомерен.
    """
    findings: list[Finding] = []
    crew_assignments = crew_assignments or {}
    if not services:
        return findings

    for service in services:
        marks = [entry for entry in entries if entry.service == service]
        latest = current(entries, service)
        _, reaction_findings = _report_reactions(
            marks, phone_reports or [], service, report_reaction_limit_seconds,
        )
        findings.extend(reaction_findings)

        # D5 — мягкая проверка памятки: если диспетчер оставил комментарий,
        # в нём должен быть назван получатель сведений. Статусы и внешние
        # звонки не используются как выдуманное доказательство.
        missing_comment_statuses = [
            SERVICE_STATUS_LABELS[mark.status]
            for mark in marks
            if mark.status in COMMENT_REQUIRED and not mark.comment.strip()
        ]
        if missing_comment_statuses:
            findings.append(_finding(
                ErrorCode.D5,
                f"{service}: к статусу не добавлены основание и сведения",
                fact="не заполнены комментарии: " + ", ".join(missing_comment_statuses),
                norm="к каждой ручной отметке добавить основание и содержание полученных сведений",
            ))

        incomplete_comment_statuses = [
            SERVICE_STATUS_LABELS[mark.status]
            for mark in marks
            if mark.status in COMMENT_REQUIRED
            and mark.comment.strip()
            and not _has_basis_and_information(mark.comment)
        ]
        if incomplete_comment_statuses:
            findings.append(_finding(
                ErrorCode.D5,
                f"{service}: комментарии к статусам не разделяют основание и сведения",
                fact="неполные комментарии: " + ", ".join(incomplete_comment_statuses),
                norm="в каждом комментарии заполнить отдельные поля «Основание» и «Сведения»",
            ))

        comments_to_check = [
            (SERVICE_STATUS_LABELS[mark.status], mark.comment.strip())
            for mark in marks
            if mark.comment.strip()
        ]
        if reply_text.strip():
            comments_to_check.append(("ответ ДДС", reply_text.strip()))
        missing_recipients = [
            f"{label}: {comment[:180]}"
            for label, comment in comments_to_check
            if not _has_recipient(comment, service, crew_assignments.get(service))
        ]
        if missing_recipients:
            findings.append(_finding(
                ErrorCode.D5,
                f"{service}: отдельный комментарий не указывает получателя сведений",
                fact="; ".join(missing_recipients)[:500],
                norm=(
                    "назвать, кому переданы сведения: бригаде, службе, "
                    "заявителю или иному адресату"
                ),
            ))

        # D1 — первичного статуса нет либо он проставлен позже норматива.
        primary = next((mark for mark in marks if mark.status in PRIMARY), None)
        if primary is None:
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

        if elapsed_ms is None or elapsed_ms > deadline_ms:
            findings.append(
                _finding(
                    ErrorCode.D1,
                    f"{service}: первичный статус проставлен с нарушением срока",
                    fact=(
                        f"прошло {elapsed_ms // 1000} с"
                        if elapsed_ms is not None
                        else "время первичной отметки не зафиксировано"
                    ),
                    norm=f"первичный статус в течение {deadline_ms // 1000} с",
                )
            )

        for mark in marks:
            # D4 — отказ без комментария.
            if mark.status in REFUSAL_COMMENT_REQUIRED and not mark.comment.strip():
                findings.append(
                    _finding(
                        ErrorCode.D4,
                        f"{service}: «{SERVICE_STATUS_LABELS[mark.status]}» без комментария",
                        fact="причина отказа не указана",
                        norm="комментарий к отказу обязателен",
                    )
                )

        # D3 — отказ от профильного происшествия, когда эталон сценария
        # требует принятия. Дубль/территориальное исключение задаются явно.
        if latest is ServiceStatus.DECLINED and expected_decision == "accept":
            findings.append(
                _finding(
                    ErrorCode.D3,
                    f"{service}: отказ от происшествия, которое в её компетенции",
                    fact="служба есть в списке оповещения по ЕКП",
                    norm="принять согласно эталону сценария",
                )
            )
        elif latest is ServiceStatus.ACCEPTED and expected_decision == "decline":
            findings.append(
                _finding(
                    ErrorCode.D2,
                    f"{service}: принято вопреки эталону сценария",
                    fact="карточка принята службой",
                    norm=expected_decision_reason or "отказать с указанной причиной",
                )
            )

        # D2 — «Принята» без назначения бригады/дальнейшего хода или ход без
        # зафиксированной бригады. Заказчик уточнил: необходимые бригады ДДС
        # выбирает вручную; канал получения докладов при этом не предписан.
        if latest is ServiceStatus.ACCEPTED and expected_decision == "accept":
            if service not in crew_assignments:
                findings.append(
                    _finding(
                        ErrorCode.D2,
                        f"{service}: «Принята» без назначения бригады и хода реагирования",
                        fact="бригада не выбрана, после приёма статусов не было",
                        norm="необходимую бригаду выбирает ДДС; ход отмечается по факту",
                    )
                )
            else:
                findings.append(
                    _finding(
                        ErrorCode.D2,
                        f"{service}: «Принята», но о реагировании ничего не отмечено",
                        fact="после приёма статусов не было",
                        norm="статус должен соответствовать фактическому состоянию заявки",
                    )
                )
        elif latest is not ServiceStatus.DECLINED and any(
            mark.status in PROGRESS for mark in marks
        ) and service not in crew_assignments:
            findings.append(
                _finding(
                    ErrorCode.D2,
                    f"{service}: ход реагирования без назначения бригады",
                    fact="статусы хода работ проставлены, бригада не выбрана",
                    norm="необходимую бригаду выбирает ДДС; ход отмечается по факту",
                )
            )

        # D6 — ход работ неполон к моменту закрытия карточки/занятия. В памятке
        # это приводит к повторным звонкам и скрывает от других служб факт реакции.
        # REFUSED — отдельный допустимый терминальный статус с обязательной причиной.
        missing_progress = [status for status in PROGRESS if status not in {m.status for m in marks}]
        if (latest not in {ServiceStatus.DECLINED, ServiceStatus.REFUSED}
                and (missing_progress or latest is not ServiceStatus.COMPLETED)):
            missing = ", ".join(SERVICE_STATUS_LABELS[status] for status in missing_progress)
            findings.append(
                _finding(
                    ErrorCode.D6,
                    f"{service}: ход реагирования не доведён до конца",
                    fact=(f"не отмечены: {missing}" if missing else "карточка не закрыта"),
                    norm=(
                        "отметить по факту начало реагирования, прибытие, проведение работ "
                        "и завершение; если работы не проводились — оформить отказ с причиной"
                    ),
                )
            )

    return findings


def dispatcher_metrics(
    state,
    deadline_ms: int,
    expected_decision: str = "accept",
    expected_decision_reason: str | None = None,
    *,
    services: list[str] | None = None,
    report_reaction_limit_seconds: int = 45,
) -> list[Metric]:
    """Числовая часть оценки ДДС; каждый проверяемый шаг имеет факт и норму.

    Веса предварительные — до утверждения методистом. Телефон — возможный
    источник информации, но не обязательный шлюз статуса.
    """
    metrics: list[Metric] = []
    for service in (services if services is not None else state.managed_services()):
        marks = [mark for mark in state.status_log if mark.service == service]
        statuses = {mark.status for mark in marks}
        reaction_metrics, _ = _report_reactions(
            marks, state.phone_reports, service, report_reaction_limit_seconds,
        )
        metrics.extend(reaction_metrics)

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
        add("dds_ack", f"первичная отметка за {deadline_ms // 1000} с",
            elapsed is not None and elapsed <= deadline_ms,
            f"{elapsed / 1000:.1f} с" if elapsed is not None else "не отмечено",
            f"≤ {deadline_ms // 1000} с")
        if primary is None:
            continue
        expected_status = (ServiceStatus.ACCEPTED if expected_decision == "accept"
                           else ServiceStatus.DECLINED)
        expected_label = "Принята" if expected_decision == "accept" else "Не принята"
        add("dds_decision", "решение по эталону сценария",
            primary.status is expected_status,
            primary.status.value,
            expected_decision_reason or f"по эталону ожидается «{expected_label}»", 2.0)
        if primary.status is ServiceStatus.DECLINED or expected_decision == "decline":
            continue
        crew = state.crew_assignments.get(service)
        add("dds_crew", "назначение бригады", bool(crew),
            crew or "бригада не выбрана",
            "необходимую бригаду выбирает ДДС вручную", 2.0)
        expected = {
            ServiceStatus.RESPONDING, ServiceStatus.ARRIVED, ServiceStatus.WORKING,
        }
        refused = ServiceStatus.REFUSED in statuses
        add("dds_progress", "ведение хода реагирования",
            expected <= statuses or refused,
            ("отказ от выполнения работ" if refused else
             ", ".join(mark.status.value for mark in marks if mark.status in expected)
             or "статусов хода нет"),
            "отметить по факту ход работ либо оформить обоснованный отказ от их выполнения", 2.0)
        add("dds_completion", "закрытие работ",
            ServiceStatus.COMPLETED in statuses or refused,
            ("завершено" if ServiceStatus.COMPLETED in statuses else
             "оформлен отказ от выполнения работ" if refused else "не завершено"),
            "зафиксировать фактическое завершение работ или отказ от их выполнения")
        notes = [mark.comment.strip() for mark in marks if mark.status in COMMENT_REQUIRED]
        notes_complete = bool(notes) and all(
            _has_basis_and_information(note) for note in notes
        )
        add("dds_reply", "основание статусов и сведения о ходе работ",
            notes_complete,
            "; ".join(notes) if notes else "комментарии к статусам не внесены",
            "к каждой ручной отметке добавить основание и содержание полученных сведений")
    return metrics
