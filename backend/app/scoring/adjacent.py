"""Оценка работы курсанта со смежными службами: вводная «бригада просит смежников».

Ответ заказчика (28.09): статусы и комментарии в карточке в первую очередь
читают другие ДДС, участвующие в ликвидации. Проверяются два действия —
сверился ли курсант с карточкой перед ответом бригаде и связался ли со
смежной службой, когда та молчит. Обе метрики детерминированные, по событиям
занятия, и обе размечаются кодом D6: неполный ход работ в карточке скрывает
от смежных служб факт реакции (классификатор D1–D6 не расширяется).
"""

from collections.abc import Sequence
from datetime import datetime

from app.domain.events import Metric
from app.domain.statuses import ADJACENT_REACTING, StatusEntry
from app.domain.taxonomy import Competency, ErrorCode, Finding, FindingSource
from app.scoring.taxonomy import METRIC_WEIGHTS

ADJACENT_REF = "памятка «Работа на АРМ-112», статусы реагирования; ответ заказчика 28.09"


def active_ms(start: datetime, end: datetime,
              pauses: Sequence[tuple[datetime, datetime]]) -> int:
    """Активное время между моментами: закрытые интервалы пауз вычитаются."""
    elapsed = int((end - start).total_seconds() * 1000)
    for pause_start, pause_end in pauses:
        overlap = (min(end, pause_end) - max(start, pause_start)).total_seconds()
        if overlap > 0:
            elapsed -= int(overlap * 1000)
    return max(0, elapsed)


def _reacted_before(entries: list[StatusEntry], service: str, moment: datetime) -> bool:
    """Реакция смежной службы в карточке к моменту: любой статус реагирования.

    Автор не важен: отметка после звонка курсанта — такая же реакция, как
    сценарная, и успокоить бригаду после неё правомерно.
    """
    return any(entry.service == service and entry.status in ADJACENT_REACTING
               and entry.at <= moment for entry in entries)


def _metric(key: str, title: str, passed: bool, fact: str, norm: str, service: str) -> Metric:
    return Metric(key=key, title=f"{service}: {title}", fact=fact, norm=norm,
                  ref=ADJACENT_REF, passed=passed, service=service,
                  weight=METRIC_WEIGHTS.get(key, 1.0))


def _finding(service: str, competency: Competency, summary: str, fact: str,
             norm: str) -> Finding:
    return Finding(service=service, code=ErrorCode.D6, source=FindingSource.DISPATCHER,
                   summary=f"{service}: {summary}", fact=fact, norm=norm,
                   ref=ADJACENT_REF, competency=competency)


def adjacent_metrics(
    card, limit_ms: int,
    pause_intervals: Sequence[tuple[datetime, datetime]] | None = None,
) -> tuple[list[Metric], list[Finding]]:
    """Метрики и отметки по вводной. Пусто — вводной не было или она не пришла.

    Исход «реагирует / молчит» — следствие таймлайна сценария, отдельной
    ручки нет. Перестраховочный звонок смежной службе при её реагировании
    ошибкой не считается: метрика звонка в этом исходе не выставляется.
    """
    request = card.scenario.crew_request
    if request is None or card.crew_request_at is None:
        return [], []
    pauses = list(pause_intervals or [])
    service = request.service
    reacting_variant = any(
        entry.service == service and entry.status in ADJACENT_REACTING
        for entry in card.scenario.adjacent_timeline
    )
    reply_delay_ms = (active_ms(card.crew_request_at, card.adjacent_reply_at, pauses)
                      if card.adjacent_reply_at is not None else None)
    call_delay_ms = (active_ms(card.crew_request_at, card.adjacent_call_at, pauses)
                     if card.adjacent_call_at is not None else None)
    limit_s = limit_ms // 1000
    metrics: list[Metric] = []
    findings: list[Finding] = []

    check_norm = ("свериться с журналом карточки и передать бригаде фактическое "
                  "состояние реагирования смежной службы")
    if card.adjacent_reply_claimed is True:
        reacted = _reacted_before(card.status_log, service, card.adjacent_reply_at)
        late = reacting_variant and reply_delay_ms is not None and reply_delay_ms > limit_ms
        if not reacted:
            fact = ("бригаде передано «смежная служба реагирует», "
                    "хотя отметок её реакции в карточке не было")
        elif late:
            fact = (f"ответ бригаде через {reply_delay_ms // 1000} с "
                    f"при лимите {limit_s} с")
        else:
            fact = f"ответ бригаде сверен с карточкой: {service} реагирует"
        metrics.append(_metric("dds_adjacent_check", "сверка с карточкой перед ответом бригаде",
                               reacted and not late, fact, check_norm, service))
        if not reacted:
            findings.append(_finding(service, Competency.CARD,
                                     "бригада успокоена без сверки с карточкой",
                                     fact, check_norm))
        elif late:
            findings.append(_finding(service, Competency.CARD,
                                     "ответ бригаде позже лимита вводной",
                                     fact, check_norm))
    elif card.adjacent_reply_claimed is False:
        if reacting_variant:
            metrics.append(_metric(
                "dds_adjacent_check", "сверка с карточкой перед ответом бригаде", True,
                "бригаде передано, что сведения будут уточнены", check_norm, service))
    elif reacting_variant:
        fact = "входящий вызов бригады остался без ответа до закрытия карточки"
        metrics.append(_metric("dds_adjacent_check",
                               "сверка с карточкой перед ответом бригаде",
                               False, fact, check_norm, service))
        findings.append(_finding(service, Competency.CARD,
                                 "запрос бригады о смежной службе проигнорирован",
                                 fact, check_norm))

    if not reacting_variant:
        call_norm = (f"при молчании смежной службы связаться с ней и ответить "
                     f"бригаде не позже {limit_s} с от вводной")
        reply_ok = reply_delay_ms is not None and reply_delay_ms <= limit_ms
        call_ok = call_delay_ms is not None and call_delay_ms <= limit_ms
        call_fact = (f"звонок «{service}» через {call_delay_ms // 1000} с"
                     if call_delay_ms is not None else f"звонка «{service}» не было")
        reply_fact = (f"ответ бригаде через {reply_delay_ms // 1000} с"
                      if reply_delay_ms is not None else "ответа бригаде не было")
        fact = f"{call_fact}; {reply_fact}"
        metrics.append(_metric("dds_adjacent_call", "связь со смежной службой при её молчании",
                               reply_ok and call_ok, fact, call_norm, service))
        if not (reply_ok and call_ok):
            summary = ("молчание смежной службы осталось без звонка"
                       if call_delay_ms is None
                       else "запрос бригады о смежной службе не закрыт в лимит")
            findings.append(_finding(service, Competency.COMMUNICATION, summary,
                                     fact, call_norm))
    return metrics, findings
