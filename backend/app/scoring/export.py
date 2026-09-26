"""Экспорт разбора занятия без обращения к внешним сервисам.

CSV - плоский журнал с пометкой раздела. PDF - читаемый протокол с переносом
длинных реплик и автоматическим разбиением на страницы.
"""

import csv
import io
import os
from html import escape
from pathlib import Path

from app.domain.events import SessionReport


def _cell(value: object) -> str:
    """Excel/LibreOffice не должны исполнять текст курсанта как формулу."""
    if value is None:
        return ""
    result = str(value)
    probe = result.lstrip(" \t\r\n\ufeff\u200b")
    if probe.startswith(("=", "+", "-", "@")) or result.startswith(("\t", "\r", "\n")):
        return "'" + result
    return result


def to_csv(report: SessionReport) -> bytes:
    """UTF-8 с BOM: кириллица открывается в Excel без выбора кодировки."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["Раздел", "№", "Поле", "Значение", "Дополнительно"])

    def row(section: str, index: object, field: str, value: object, detail: object = "") -> None:
        writer.writerow([_cell(part) for part in (section, index, field, value, detail)])

    row("Занятие", "", "ID", report.session_id)
    row("Занятие", "", "Сценарий", report.scenario_id)
    row("Занятие", "", "Режим", report.mode.value)
    row("Занятие", "", "Попытка", report.attempt)
    row("Критерии", "", "Лимит решения ДДС, с", report.criteria.decision_time_limit_seconds)
    row("Критерии", "", "Допустимо ошибок", report.criteria.allowed_errors)
    row("Критерии", "", "Грамматика обязательна", "да" if report.criteria.require_correct_grammar else "нет")
    row("Критерии", "", "Переопределения весов", report.criteria.score_weights or "стандартные")
    row("Критерии", "", "Нарушенных метрик", report.failed_metrics)
    row("Критерии", "", "Зачёт", "да" if report.passed else "нет")
    row("Оценка", "", "Автоматическая", report.score_auto)
    row("Оценка", "", "Итоговая", report.score_final)
    row("Оценка", "", "Изменена преподавателем", report.overridden_by)
    row("Оценка", "", "Причина изменения", report.override_comment)

    for number, item in enumerate(report.metrics, 1):
        credit = f"; оценочный вклад: {item.credit:.0%}" if item.credit is not None else ""
        row("Метрики", number, item.title, item.fact,
            f"Норматив: {item.norm}; результат: {'да' if item.passed else 'нет'}; "
            f"вес: {item.weight:g}{credit}; источник: {item.ref or ''}")
    for number, item in enumerate(report.findings, 1):
        row("Ошибки", number, item.code.value, item.summary, f"Факт: {item.fact}; норматив: {item.norm or ''}; источник: {item.ref or ''}")
    for number, item in enumerate(report.competencies, 1):
        row("Компетенции", number, item.competency, item.value)
    for number, item in enumerate(report.transcript, 1):
        row("Транскрипт", number, item.speaker.value, item.text, item.at.isoformat())
    for number, item in enumerate(report.reference_questions, 1):
        row("Эталонные вопросы", number, item.checklist_id, item.question)
    for number, item in enumerate(report.missed_checklist, 1):
        row("Пропущенные вопросы", number, "Пункт", item)
    for number, item in enumerate(report.hints_used, 1):
        row("Подсказки", number, item.checklist_id, item.question, item.at.isoformat())
    if report.self_assessment:
        row("Самооценка", "", "Пропущено по мнению курсанта", ", ".join(report.self_assessment.missed))
        row("Самооценка", "", "Комментарий", report.self_assessment.comment)
    if report.self_assessment_diff:
        for label, values in (
            ("Заметил", report.self_assessment_diff.noticed),
            ("Не заметил", report.self_assessment_diff.unnoticed),
            ("Отметил ошибочно", report.self_assessment_diff.overcautious),
        ):
            row("Сверка самооценки", "", label, ", ".join(values))
    for number, item in enumerate(report.notes, 1):
        row("Пометки преподавателя", number, item.transcript_ref, item.text, item.author)
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def _font_paths() -> tuple[Path, Path | None]:
    """Находить TTF с кириллицей явно, без молчаливой подмены Helvetica."""
    custom = os.environ.get("LCT_PDF_FONT")
    if custom:
        path = Path(custom)
        if not path.is_file():
            raise RuntimeError(f"LCT_PDF_FONT не найден: {path}")
        return path, None
    # DejaVu лежит в пакете: демо без Docker идёт на хосте, где системного шрифта
    # может не быть, а экспорт PDF должен работать офлайн без доустановки.
    bundled = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    candidates = (
        (str(bundled / "DejaVuSans.ttf"), str(bundled / "DejaVuSans-Bold.ttf")),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    )
    for regular, bold in candidates:
        if Path(regular).is_file():
            return Path(regular), Path(bold) if Path(bold).is_file() else None
    raise RuntimeError("Не найден TTF-шрифт с кириллицей; установите DejaVu Sans или задайте LCT_PDF_FONT")


def to_pdf(report: SessionReport) -> bytes:
    """Собрать многостраничный PDF с кириллицей и переносом длинных текстов."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

    regular_path, bold_path = _font_paths()
    regular_name = "LCTReportRegular"
    bold_name = "LCTReportBold"
    if regular_name not in pdfmetrics.getRegisteredFontNames():
        regular = TTFont(regular_name, str(regular_path))
        if not all(ord(char) in regular.face.charToGlyph for char in "Отчёт курсанта Ёж"):
            raise RuntimeError(f"Шрифт не поддерживает кириллицу: {regular_path}")
        pdfmetrics.registerFont(regular)
        pdfmetrics.registerFont(TTFont(bold_name, str(bold_path or regular_path)))
        pdfmetrics.registerFontFamily(regular_name, normal=regular_name, bold=bold_name)

    base = ParagraphStyle(
        "body", fontName=regular_name, fontSize=9, leading=13,
        textColor=colors.HexColor("#18202a"), spaceAfter=5,
        allowWidows=0, allowOrphans=0, splitLongWords=1,
    )
    title_style = ParagraphStyle(
        "title", parent=base, fontName=bold_name, fontSize=17, leading=22,
        textColor=colors.HexColor("#193b56"), spaceAfter=10,
    )
    heading = ParagraphStyle(
        "heading", parent=base, fontName=bold_name, fontSize=12, leading=17,
        textColor=colors.HexColor("#193b56"), spaceBefore=13, spaceAfter=5,
        keepWithNext=True,
    )
    muted = ParagraphStyle("muted", parent=base, fontSize=8, textColor=colors.HexColor("#5d6873"))

    def p(value: object, style=base):
        # XML-экранирование необходимо: реплики и пометки вводит человек.
        safe = escape("" if value is None else str(value)).replace("\n", "<br/>")
        return Paragraph(safe or "-", style)

    story = [p("Отчёт по учебному занятию", title_style)]
    for label, value in (
        ("Идентификатор", report.session_id),
        ("Сценарий", report.scenario_id),
        ("Режим", report.mode.value),
        ("Попытка", report.attempt),
        ("Лимит решения ДДС", f"{report.criteria.decision_time_limit_seconds} с"),
        ("Допустимо ошибок", report.criteria.allowed_errors),
        ("Грамматика обязательна", "да" if report.criteria.require_correct_grammar else "нет"),
        ("Переопределения весов", report.criteria.score_weights or "стандартные"),
        ("Нарушенных метрик", report.failed_metrics),
        ("Результат", "зачёт" if report.passed else "не зачтено"),
        ("Автоматическая оценка", report.score_auto),
        ("Итоговая оценка", report.score_final),
    ):
        story.append(Paragraph(f"<b>{escape(label)}:</b> {escape(str(value))}", base))
    if report.overridden_by:
        story.append(Paragraph(f"<b>Оценка изменена:</b> {escape(report.overridden_by)}", base))
    if report.override_comment:
        story.append(p(f"Причина изменения: {report.override_comment}"))
    story.extend([Spacer(1, 7), HRFlowable(width="100%", color=colors.HexColor("#aeb8c2"))])

    def section(label: str):
        story.append(p(label, heading))

    section("Метрики")
    if not report.metrics:
        story.append(p("Нет данных", muted))
    for item in report.metrics:
        credit = f", оценочный вклад {item.credit:.0%}" if item.credit is not None else ""
        story.append(p(f"{item.title} - {'выполнено' if item.passed else 'нарушено'} "
                       f"(вес {item.weight:g}{credit})"))
        story.append(p(f"Факт: {item.fact}. Норматив: {item.norm}. {item.ref or ''}", muted))

    section("Выявленные ошибки")
    if not report.findings:
        story.append(p("Ошибок не выявлено", muted))
    for item in report.findings:
        story.append(p(f"{item.code.value} - {item.summary}"))
        story.append(p(f"Факт: {item.fact}. Норматив: {item.norm or 'не указан'}. Источник: {item.ref or 'не указан'}", muted))

    section("Компетенции")
    for item in report.competencies:
        story.append(p(f"{item.competency}: {item.value:g}"))
    if not report.competencies:
        story.append(p("Нет данных", muted))

    section("Транскрипт")
    if not report.transcript:
        story.append(p("Реплик нет", muted))
    for item in report.transcript:
        story.append(p(f"{item.at.isoformat()} - {item.speaker.value}", muted))
        story.append(p(item.text))

    section("Эталонные вопросы")
    for item in report.reference_questions:
        story.append(p(f"{item.checklist_id}: {item.question}"))
    if not report.reference_questions:
        story.append(p("Нет данных", muted))
    if report.missed_checklist:
        section("Пропущенные пункты")
        for item in report.missed_checklist:
            story.append(p(item))

    if report.hints_used:
        section("Использованные подсказки")
        for item in report.hints_used:
            story.append(p(f"{item.at.isoformat()} - {item.checklist_id}: {item.question}"))

    if report.self_assessment:
        section("Самооценка")
        story.append(p("Пропущено по мнению курсанта: " + ", ".join(report.self_assessment.missed)))
        story.append(p("Комментарий: " + report.self_assessment.comment))
    if report.self_assessment_diff:
        for label, items in (
            ("Заметил", report.self_assessment_diff.noticed),
            ("Не заметил", report.self_assessment_diff.unnoticed),
            ("Отметил ошибочно", report.self_assessment_diff.overcautious),
        ):
            story.append(p(f"{label}: {', '.join(items) or '-'}"))
    if report.notes:
        section("Пометки преподавателя")
        for item in report.notes:
            story.append(p(f"{item.transcript_ref} - {item.author}: {item.text}"))

    output = io.BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=A4, leftMargin=42, rightMargin=42,
        topMargin=45, bottomMargin=44,
        title="Отчёт по учебному занятию", author="Учебный симулятор 112",
    )

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont(regular_name, 8)
        canvas.setFillColor(colors.HexColor("#5d6873"))
        canvas.drawString(42, 25, "Учебный симулятор 112")
        canvas.drawRightString(A4[0] - 42, 25, f"Стр. {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def certificate_pdf(
    *, trainee_name: str, trainee_id: object, group_name: str | None,
    attempts: int, average_score: float, issued_at: object,
) -> bytes:
    """Одностраничный сертификат по фактически сохранённым результатам."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    regular_path, bold_path = _font_paths()
    regular_name = "LCTCertificateRegular"
    bold_name = "LCTCertificateBold"
    if regular_name not in pdfmetrics.getRegisteredFontNames():
        regular = TTFont(regular_name, str(regular_path))
        if not all(ord(char) in regular.face.charToGlyph for char in "Сертификат Ёж"):
            raise RuntimeError(f"Шрифт не поддерживает кириллицу: {regular_path}")
        pdfmetrics.registerFont(regular)
        pdfmetrics.registerFont(TTFont(bold_name, str(bold_path or regular_path)))

    title = ParagraphStyle(
        "certificate-title", fontName=bold_name, fontSize=28, leading=34,
        alignment=TA_CENTER, textColor=colors.HexColor("#193b56"), spaceAfter=20,
    )
    body = ParagraphStyle(
        "certificate-body", fontName=regular_name, fontSize=14, leading=21,
        alignment=TA_CENTER, textColor=colors.HexColor("#18202a"), spaceAfter=10,
    )
    name_style = ParagraphStyle(
        "certificate-name", parent=body, fontName=bold_name, fontSize=22, leading=28,
        textColor=colors.HexColor("#a24816"), spaceAfter=18,
    )
    small = ParagraphStyle(
        "certificate-small", parent=body, fontSize=9, leading=13,
        textColor=colors.HexColor("#5d6873"),
    )
    safe_name = escape(trainee_name)
    safe_group = escape(group_name or "без группы")
    safe_date = escape(str(issued_at))
    story = [
        Spacer(1, 34),
        Paragraph("СЕРТИФИКАТ", title),
        Paragraph("о прохождении учебных занятий в локальном тренажёре системы-112 и ДДС", body),
        Spacer(1, 12),
        Paragraph(safe_name, name_style),
        Paragraph(
            f"Выполнено занятий: <b>{attempts}</b> · средняя итоговая оценка: "
            f"<b>{average_score:.1f} из 100</b>", body,
        ),
        Paragraph(f"Учебная группа: {safe_group}", body),
        Spacer(1, 28),
        Paragraph(f"Дата формирования: {safe_date}", small),
        Paragraph(f"Идентификатор курсанта: {escape(str(trainee_id))}", small),
        Paragraph(
            "Документ сформирован локально по сохранённым результатам и не заменяет "
            "квалификационный документ установленного образца.", small,
        ),
    ]
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=landscape(A4), leftMargin=60, rightMargin=60,
        topMargin=45, bottomMargin=45, title="Сертификат о прохождении обучения",
        author="Учебный симулятор 112",
    )
    document.build(story)
    return output.getvalue()
