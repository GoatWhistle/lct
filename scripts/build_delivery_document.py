#!/usr/bin/env python3
"""Build the delivery documentation required by task 09 as DOCX.

The text is intentionally explicit about verified features and open acceptance
items. Run through the repository environment with python-docx available:

    uv run --no-project --with python-docx python scripts/build_delivery_document.py
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "delivery"
OUT_DOCX = OUT_DIR / "lct-hack-accompanying-documentation.docx"

NAVY = "24455E"
PALE_BLUE = "EAF1F5"
PALE_GRAY = "F5F6F7"
BORDER = "D9D9D9"
MID_GRAY = RGBColor(90, 98, 104)


def set_cell_fill(cell, color: str) -> None:
    properties = cell._tc.get_or_add_tcPr()
    shading = properties.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        properties.append(shading)
    shading.set(qn("w:fill"), color)


def set_cell_margins(cell, top=110, start=120, bottom=110, end=120) -> None:
    properties = cell._tc.get_or_add_tcPr()
    margins = properties.first_child_found_in("w:tcMar")
    if margins is None:
        margins = OxmlElement("w:tcMar")
        properties.append(margins)
    for edge, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = margins.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            margins.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_table_borders(table) -> None:
    properties = table._tbl.tblPr
    borders = properties.find(qn("w:tblBorders"))
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        properties.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = borders.find(qn(f"w:{edge}"))
        if element is None:
            element = OxmlElement(f"w:{edge}")
            borders.append(element)
        element.set(qn("w:val"), "single")
        element.set(qn("w:sz"), "5")
        element.set(qn("w:color"), BORDER)


def set_repeat_table_header(row) -> None:
    properties = row._tr.get_or_add_trPr()
    repeat = OxmlElement("w:tblHeader")
    repeat.set(qn("w:val"), "true")
    properties.append(repeat)


def prevent_row_split(row) -> None:
    properties = row._tr.get_or_add_trPr()
    cant_split = OxmlElement("w:cantSplit")
    properties.append(cant_split)


def keep_with_next(paragraph) -> None:
    paragraph.paragraph_format.keep_with_next = True


def add_page_number(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend((begin, instruction, separate, end))


def configure_document(document: Document) -> None:
    section = document.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.top_margin = Inches(0.72)
    section.bottom_margin = Inches(0.7)
    section.left_margin = Inches(0.82)
    section.right_margin = Inches(0.72)
    section.header_distance = Inches(0.3)
    section.footer_distance = Inches(0.32)

    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(10.6)
    normal.font.color.rgb = RGBColor(0, 0, 0)
    normal.paragraph_format.space_after = Pt(5.5)
    normal.paragraph_format.line_spacing = 1.12

    title = document.styles["Title"]
    title.font.name = "Arial"
    title.font.size = Pt(27)
    title.font.bold = True
    title.font.color.rgb = RGBColor(0, 0, 0)
    title.paragraph_format.space_after = Pt(14)
    title.paragraph_format.line_spacing = 1.0
    title_properties = title.element.get_or_add_pPr()
    title_border = title_properties.find(qn("w:pBdr"))
    if title_border is not None:
        title_properties.remove(title_border)

    for name, size, before, after in (
        ("Heading 1", 17, 12, 6),
        ("Heading 2", 13.5, 9, 4),
        ("Heading 3", 11.5, 7, 3),
    ):
        style = document.styles[name]
        style.font.name = "Arial"
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True

    if "Code" not in document.styles:
        code = document.styles.add_style("Code", WD_STYLE_TYPE.PARAGRAPH)
    else:
        code = document.styles["Code"]
    code.font.name = "Menlo"
    code.font.size = Pt(8.5)
    code.paragraph_format.left_indent = Inches(0.18)
    code.paragraph_format.right_indent = Inches(0.12)
    code.paragraph_format.space_before = Pt(3)
    code.paragraph_format.space_after = Pt(6)
    code.paragraph_format.line_spacing = 1.0

    for current in document.sections:
        header = current.header.paragraphs[0]
        header.text = "Сопроводительная документация  lct hack"
        header.style = document.styles["Normal"]
        header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for run in header.runs:
            run.font.size = Pt(8)
            run.font.color.rgb = MID_GRAY
        footer = current.footer.paragraphs[0]
        add_page_number(footer)
        for run in footer.runs:
            run.font.size = Pt(8)
            run.font.color.rgb = MID_GRAY


def add_paragraph(document: Document, text: str, *, bold_lead: str | None = None):
    paragraph = document.add_paragraph()
    if bold_lead and text.startswith(bold_lead):
        paragraph.add_run(bold_lead).bold = True
        paragraph.add_run(text[len(bold_lead):])
    else:
        paragraph.add_run(text)
    return paragraph


def add_bullets(document: Document, items: list[str]) -> None:
    for item in items:
        paragraph = document.add_paragraph(style="List Bullet")
        paragraph.add_run(item)
        paragraph.paragraph_format.space_after = Pt(2.5)


def add_numbered(document: Document, items: list[str]) -> None:
    for index, item in enumerate(items, start=1):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Inches(0.28)
        paragraph.paragraph_format.first_line_indent = Inches(-0.24)
        paragraph.add_run(f"{index}.  {item}")
        paragraph.paragraph_format.space_after = Pt(3)


def add_code(document: Document, code: str) -> None:
    paragraph = document.add_paragraph(style="Code")
    paragraph.add_run(code)
    paragraph.paragraph_format.keep_together = True


def add_table(document: Document, headers: list[str], rows: list[list[str]], widths: list[float] | None = None):
    if document.paragraphs:
        document.paragraphs[-1].paragraph_format.keep_with_next = True
    table = document.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    set_table_borders(table)
    set_repeat_table_header(table.rows[0])
    prevent_row_split(table.rows[0])
    for index, header in enumerate(headers):
        cell = table.rows[0].cells[index]
        set_cell_fill(cell, NAVY)
        set_cell_margins(cell)
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        paragraph = cell.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.keep_with_next = True
        run = paragraph.add_run(header)
        run.bold = True
        run.font.color.rgb = RGBColor(255, 255, 255)
        run.font.size = Pt(9)
        if widths:
            cell.width = Inches(widths[index])
    for row_index, row_values in enumerate(rows):
        data_row = table.add_row()
        prevent_row_split(data_row)
        cells = data_row.cells
        for index, value in enumerate(row_values):
            cell = cells[index]
            set_cell_margins(cell)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            if row_index % 2:
                set_cell_fill(cell, PALE_BLUE)
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            paragraph.paragraph_format.line_spacing = 1.02
            run = paragraph.add_run(value)
            run.font.size = Pt(8.8)
            if widths:
                cell.width = Inches(widths[index])
    document.add_paragraph().paragraph_format.space_after = Pt(1)
    return table


def add_section(document: Document, title: str, level: int = 1) -> None:
    document.add_heading(title, level=level)


def add_cover(document: Document) -> None:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Inches(0.7)
    paragraph.paragraph_format.space_after = Pt(8)
    run = paragraph.add_run("ЗАДАЧА 9")
    run.bold = True
    run.font.size = Pt(11)
    run.font.color.rgb = MID_GRAY

    title = document.add_paragraph(style="Title")
    title.add_run("Учебный симулятор работы оператора 112 и диспетчера ДДС")

    subtitle = document.add_paragraph()
    subtitle.add_run("Сопроводительная документация по архитектуре сборке установке и эксплуатации").bold = True
    subtitle.runs[0].font.size = Pt(14)
    subtitle.paragraph_format.space_after = Pt(22)

    add_table(
        document,
        ["Параметр", "Значение"],
        [
            ["Репозиторий", "lct-hack"],
            ["Версия документа", "23 сентября 2026 года"],
            ["Назначение", "Локальное обучение операторов 112 и диспетчеров ДДС"],
            ["Основной контур", "Готовая карточка 112 → ДДС → бригада → разбор"],
            ["Режим поставки", "Локальная сеть учебного центра без внешних сервисов"],
        ],
        [1.55, 5.0],
    )

    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(22)
    paragraph.add_run(
        "Документ описывает реализованный прототип, порядок его развёртывания и границы подтверждённого соответствия. "
        "Основной учебный путь работает локально без голосовых моделей. Локальный SIP сервер, полная приёмка на целевом "
        "оборудовании и испытание со 100 браузерами остаются отдельными обязательными работами и не заявлены выполненными."
    )
    paragraph.paragraph_format.line_spacing = 1.2
    document.add_page_break()


def build() -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    document = Document()
    configure_document(document)
    add_cover(document)

    add_section(document, "Содержание")
    add_numbered(document, [
        "Назначение и подтверждённый объём",
        "Функциональная архитектура",
        "Компонентная архитектура",
        "Методы обработки данных",
        "Пользовательские роли и учебные маршруты",
        "Локальные модели и жёсткий протокол",
        "Данные заказчика",
        "Сборка и установка",
        "Эксплуатация",
        "Безопасность резервное копирование и офлайн режим",
        "Проверки и показатели",
        "Ограничения и незакрытые требования",
        "Библиотеки и лицензии",
        "Приёмочный чек лист",
    ])
    add_paragraph(document, "Пути и команды приведены относительно корня репозитория. Команды PowerShell выполняются без Makefile по указанным Python и Docker эквивалентам.")

    add_section(document, "Назначение и подтверждённый объём")
    add_paragraph(document, "Сервис предназначен для локального обучения операторов системы 112 и диспетчеров дежурно диспетчерских служб. Преподаватель назначает карточку или сценарий, курсант выполняет упражнение в интерфейсе, система фиксирует действия и нормативы, после чего формирует разбор и отчёт.")
    add_paragraph(document, "Приоритетный маршрут прототипа начинается с готовой карточки. Диспетчер ДДС принимает или отклоняет её, выбирает бригаду, проходит учебный разговор со старшим группы, фиксирует доклады и статусы. Дополнительно реализованы текстовая вводная оператора 112, сквозной маршрут 112 → ДДС и экспериментальный голосовой режим.")
    add_table(document, ["Функция", "Состояние", "Доказательство"], [
        ["Готовая карточка ДДС", "Реализовано", "WebSocket маршрут и тесты карточки"],
        ["96 вызовов заказчика", "Реализовано", "32 билета по три вызова"],
        ["Классификатор ЕКП", "Реализовано", "1283 строки признаков и списки оповещения"],
        ["ИИ формирователь", "Реализовано", "Локальный Qwen с серверной валидацией"],
        ["Грамматика ответа", "Реализовано", "Правила и необязательный локальный Vikhr"],
        ["SIP и VoIP", "Не реализовано", "В ДДС используется учебная событийная имитация"],
    ], [2.1, 1.25, 3.2])

    add_section(document, "Функциональная архитектура")
    add_paragraph(document, "Система разделяет подготовку занятия, выполнение упражнения и разбор. Преподаватель управляет содержанием и критериями, курсант работает только со своим назначением, администратор обслуживает локальный стенд.")
    add_table(document, ["Этап", "Вход", "Обработка", "Результат"], [
        ["Подготовка", "Сценарий или карточка", "Выбор режима курсанта таймера и критериев", "Идентификатор занятия и ссылки на АРМ"],
        ["Выполнение", "Действия курсанта", "Проверка переходов таймеров полей и статусов", "Журнал событий и состояние сессии"],
        ["Оценка", "События и эталон", "Детерминированные метрики и настраиваемые веса", "Балл ошибки зачёт или незачёт"],
        ["Разбор", "Отчёт занятия", "Сопоставление с эталоном и агрегация", "Экран CSV и PDF"],
    ], [1.2, 1.55, 2.35, 1.65])
    add_paragraph(document, "Ключевой маршрут ДДС", bold_lead="Ключевой маршрут ДДС")
    add_numbered(document, [
        "Преподаватель выбирает карточку, режим, курсанта и критерии занятия.",
        "Backend создаёт сессию и сразу подготавливает замороженную карточку ДДС.",
        "АРМ ДДС показывает реестр, карточку и таймер решения.",
        "Курсант принимает или мотивированно отклоняет карточку и выбирает бригаду.",
        "Учебный телефон проверяет передачу адреса, сути происшествия и задачи, затем подтверждение выезда и доклады.",
        "Автомат статусов разрешает только допустимые переходы и пишет события в журнал.",
        "После завершения система рассчитывает метрики и формирует разбор.",
    ])

    add_section(document, "Компонентная архитектура")
    add_table(document, ["Компонент", "Технология", "Ответственность"], [
        ["Frontend", "React TypeScript Vite", "АРМ преподавателя курсанта ДДС монитор администратора профиль и аналитика"],
        ["Backend", "FastAPI asyncio WebSocket", "Сессии протоколы роли оценка отчёты и API"],
        ["База данных", "PostgreSQL 16", "Пользователи журнал занятия отчёты сценарии и аудит"],
        ["Локальная LLM", "llama.cpp Qwen3 1.7B", "Формирование реплик и черновиков сценариев"],
        ["Русский контроль", "llama.cpp Vikhr 1B", "Необязательная проверка грамматики в строгом JSON"],
        ["STT", "whisper.cpp Whisper small", "Экспериментальное распознавание голосового режима"],
        ["Жёсткий протокол", "Python Pydantic", "ЕКП раскрытие фактов нормативы статусы оценка и валидация"],
    ], [1.35, 1.65, 3.35])
    add_paragraph(document, "Связи компонентов")
    add_table(document, ["Источник", "Канал", "Назначение"], [
        ["Браузер → Backend", "HTTP и WebSocket", "Команды формы состояние занятия и наблюдение"],
        ["Backend → PostgreSQL", "asyncpg через SQLAlchemy", "Транзакционное хранение и аудит"],
        ["Backend → Qwen или Vikhr", "OpenAI совместимый HTTP на loopback", "Локальный текстовый вывод"],
        ["Браузер ↔ Backend", "Бинарные WebSocket кадры", "Экспериментальный аудиопоток"],
    ], [1.55, 2.0, 2.8])
    add_paragraph(document, "Живое состояние сессий хранится в одном процессе backend. Поэтому рабочая конфигурация использует один uvicorn worker. Горизонтальное масштабирование потребует вынести состояние и fan out в общий локальный слой; в текущем прототипе это ограничение явно сохранено.")

    add_section(document, "Методы обработки данных")
    add_section(document, "Сценарии и эталон", 2)
    add_paragraph(document, "YAML сценарий проверяется строгими Pydantic схемами. Неизвестные поля запрещены. Загрузчик связывает факты с вопросами, проверяет адрес и пострадавших, выводит код происшествия и список оповещения из классификатора ЕКП. Черновик преподавателя не попадает в учебную библиотеку до отдельной проверки и утверждения.")
    add_section(document, "Карточка и события", 2)
    add_paragraph(document, "Backend принимает команды через типизированные WebSocket события. Авторитетное состояние находится на сервере. Клиент не может сам поставить произвольный статус или изменить выданную карточку после передачи. Каждое значимое действие получает серверное время и попадает в журнал занятия.")
    add_section(document, "Оценка", 2)
    add_paragraph(document, "Числовой результат рассчитывается по воспроизводимым метрикам: заполненность обязательных полей, адрес, пострадавшие, классификация, своевременность, решение ДДС, выбор бригады, этапы связи и корректность статусов. Сценарий задаёт веса от 0 до 10. Преподаватель задаёт лимит решения, допустимое число нарушенных метрик и участие грамматики в зачёте. Нормативы ГОСТ не редактируются.")
    add_section(document, "Отчётность", 2)
    add_paragraph(document, "Система формирует экранный отчёт, CSV и PDF. В отчёте сохраняются критерии занятия, итоговый балл, нарушенные метрики, временные показатели и статус зачёта. Групповая аналитика агрегирует сохранённые попытки и показывает частые ошибки и рекомендации по правилам.")

    add_section(document, "Пользовательские роли и учебные маршруты")
    add_table(document, ["Роль", "Разрешённые действия", "Ограничения"], [
        ["Администратор", "Пользователи роли блокировка состояние сервисов аудит и резервные копии", "Не изменяет активное занятие и оценки"],
        ["Преподаватель", "Сценарии задания критерии наблюдение директивы отчёты и аналитика", "Не меняет системные настройки"],
        ["Курсант", "Назначенное упражнение подсказки по режиму самооценка и собственный профиль", "Не видит эталон чужие результаты и админ функции"],
        ["Наблюдатель", "Монитор хода занятия и итогового разбора", "Не отправляет учебные команды"],
    ], [1.25, 3.1, 2.35])
    add_table(document, ["Упражнение", "Начало", "Завершение"], [
        ["Диспетчер ДДС", "Готовая карточка от преподавателя", "Статусы реагирования связь и итоговый доклад"],
        ["Оператор 112 текст", "Текстовая вводная", "Заполненная и переданная карточка КИО"],
        ["Связка 112 → ДДС", "Вводная оператора", "Карточка проходит обе роли в одной сессии"],
        ["Оператор 112 голос", "Экспериментальный вызов", "Карточка и разбор разговора"],
    ], [1.7, 2.25, 2.75])

    add_section(document, "Локальные модели и жёсткий протокол")
    add_paragraph(document, "Основной маршрут ДДС не требует нейросети. Для дополнительных функций все модели запускаются на том же компьютере или в локальной сети без облачного API.")
    add_table(document, ["Модель", "Назначение", "Граница ответственности"], [
        ["Qwen3 1.7B", "Диалог заявителя и формирование черновика", "Не назначает ЕКП службы нормативы статусы или баллы"],
        ["Vikhr 1B", "Русский контроль свободного ответа", "Не проверяет факты и действия"],
        ["Whisper small", "STT в экспериментальном голосовом режиме", "Не требуется для MVP ДДС"],
        ["Правила 112", "Протокол карточки и ДДС", "Единственный источник решений обязательных для оценки"],
    ], [1.45, 2.25, 3.0])
    add_paragraph(document, "Ответ Qwen принимается только после серверной проверки. Сервер запрещает управляющие токены, скрытые факты, новые числа, плейсхолдеры и рассуждения модели. Для нового сценария модель создаёт короткие поля отдельными запросами; сервер связывает адрес и число пострадавших с эталоном и заново валидирует полный сценарий. Публикация требует действия преподавателя.")
    add_paragraph(document, "Файлы моделей не входят в Git. Скрипт загрузки фиксирует имена, размеры и SHA 256. Для переноса на Windows используются те же GGUF и GGML веса, но отдельные Windows сборки llama.cpp и whisper.cpp. Виртуальное окружение между macOS Linux и Windows не переносится.")

    add_section(document, "Данные заказчика")
    add_table(document, ["Источник", "Использование в системе"], [
        ["Памятка АРМ 112 для ДДС", "Статусы карточки и реагирования нормативы обязательные поля и терминология"],
        ["Классификатор происшествий v046.24", "Иерархия признаков итоговые коды и списки оповещения"],
        ["Билеты задачи по Системе 112", "32 билета и 96 учебных вызовов"],
        ["Скриншоты АРМ и карточки", "Композиция реестра карточки статусов и справочных полей"],
        ["Полное техническое задание", "Роли функции нефункциональные требования и форматы сдачи"],
        ["Q and A и письменные уточнения", "Приоритет готовой карточки ДДС и допустимость учебной имитации связи в MVP"],
    ], [2.45, 4.3])
    add_paragraph(document, "Исходные файлы хранятся в каталоге docs/task09-materials и сокращённом наборе docs/spec/source. Текстовая расшифровка билетов приведена в docs/spec/TICKETS.md. Версия классификатора остаётся рабочей и должна быть подтверждена заказчиком перед промышленной эксплуатацией.")

    add_section(document, "Сборка и установка")
    add_section(document, "Требования к стенду", 2)
    add_bullets(document, [
        "Сервер целевого класса по ТЗ: процессор от 6 ядер, оперативная память от 32 ГБ, NVMe SSD от 512 ГБ.",
        "Docker Desktop или Docker Engine с Compose v2 для полного режима.",
        "Для нативных моделей: Python 3.11, uv, llama.cpp и при необходимости whisper.cpp.",
        "АРМ: современный Chromium браузер; для экспериментального голосового режима нужна гарнитура.",
    ])
    add_section(document, "Полный режим с PostgreSQL", 2)
    add_numbered(document, [
        "Клонировать репозиторий и перейти в его корень.",
        "Скопировать .env.example в .env и заменить SESSION_SECRET и пароли базы до доступа из локальной сети.",
        "Собрать и запустить контейнеры командой docker compose up --build.",
        "После готовности backend создать пользователей командой docker compose exec backend python scripts/users.py.",
        "Открыть http://localhost:5173 и войти под созданной учётной записью.",
        "Проверить http://localhost:8000/api/health и экран администратора.",
    ])
    add_code(document, "docker compose up --build\ndocker compose exec backend python scripts/users.py\ncurl -f http://localhost:8000/api/health")
    add_paragraph(document, "Контейнер backend при старте выполняет миграции Alembic и загружает базовую библиотеку сценариев. Данные PostgreSQL сохраняются в volume pgdata.")
    add_section(document, "Офлайн перенос", 2)
    add_code(document, "docker compose pull postgres\ndocker compose build backend frontend\ndocker save -o lct-hack-images.tar postgres:16-alpine lct-hack-backend:local lct-hack-frontend:local")
    add_paragraph(document, "На изолированном стенде загрузить архив командой docker load -i lct-hack-images.tar, затем выполнить docker compose up --pull never --no-build. Репозиторий и каталог scenarios переносятся вместе с архивом. Холодная сборка зависимостей без заранее подготовленных образов не заявлена.")
    add_section(document, "Быстрая демонстрация без базы", 2)
    add_code(document, "make demo-lite\nVITE_DEMO_MODE=true VITE_BACKEND_URL=http://127.0.0.1:8112 \\\nVITE_WS_BACKEND_URL=ws://127.0.0.1:8112 npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173")
    add_paragraph(document, "Режим demo lite хранит занятия и черновики только в памяти. Он предназначен для показа интерфейса, а не для приёмки хранения, аудита или резервного копирования.")
    add_section(document, "Локальные модели", 2)
    add_code(document, "make local-models\nmake local-llm mode=both\nmake local-stt")
    add_paragraph(document, "Qwen слушает 127.0.0.1:18080, Vikhr 127.0.0.1:18081, Whisper 127.0.0.1:18082. STT и голос выключены по умолчанию. Подробная подготовка Windows приведена в docs/arch/LOCAL-MODELS.md.")

    add_section(document, "Эксплуатация")
    add_section(document, "Проведение занятия ДДС", 2)
    add_numbered(document, [
        "Преподаватель открывает страницу instructor и выбирает категорию билет или карточку.",
        "Выбирает упражнение Диспетчер ДДС, режим, курсанта и критерии.",
        "Нажимает Начать занятие и передаёт курсанту ссылку АРМ ДДС.",
        "Курсант принимает карточку, выбирает бригаду и выполняет этапы связи.",
        "Преподаватель наблюдает состояние, а после завершения открывает разбор и экспорт.",
    ])
    add_section(document, "Формирование сценария с Qwen", 2)
    add_numbered(document, [
        "Запустить Qwen и открыть раздел ИИ формирователя на странице преподавателя.",
        "Выбрать утверждённый каркас категории и структуры опроса.",
        "Ввести описание и нажать Создать новый сюжет и эталон либо Создать вариант ИИ.",
        "Проверить первую реплику, факты, адрес, пострадавших и эталон.",
        "При необходимости дать комментарий для переработки или отредактировать JSON.",
        "Нажать Проверить и только после успешной проверки Утвердить.",
    ])
    add_section(document, "Резервное копирование", 2)
    add_paragraph(document, "Команда make backup вызывает backend/scripts/backup.py и создаёт дамп PostgreSQL. Для выполнения требования о ежедневной периодичности команду нужно поставить в планировщик конкретного стенда. Восстановление проверяется на отдельной базе до ввода стенда в эксплуатацию.")

    add_section(document, "Безопасность резервное копирование и офлайн режим")
    add_table(document, ["Мера", "Реализация", "Действие перед эксплуатацией"], [
        ["Аутентификация", "Локальные учётные записи и cookie сессии", "Заменить SESSION_SECRET и отключить DEV_AUTH_BYPASS"],
        ["Пароли", "Argon2 хеширование", "Сменить автоматически созданные пароли"],
        ["RBAC", "Роли администратора преподавателя и курсанта", "Проверить матрицу на приёмке"],
        ["Аудит", "Значимые действия пишутся в PostgreSQL", "Утвердить срок хранения и ротацию"],
        ["Резервные копии", "Скрипт pg_dump", "Настроить ежедневный планировщик и тест восстановления"],
        ["Внешняя сеть", "OFFLINE запрещает внешние URL моделей", "Запускать через заранее подготовленные образы"],
        ["TLS", "В коде приложения не завершён", "Поставить локальный reverse proxy и сертификаты"],
    ], [1.4, 2.65, 2.65])
    add_paragraph(document, "Прототип не проходил формальную проверку по 152 ФЗ, анализ уязвимостей и аттестацию защищённого контура. До работы с реальными персональными данными необходимо определить модель угроз, владельца данных, сроки хранения, порядок удаления и требования к журналам.")

    add_section(document, "Проверки и показатели")
    add_table(document, ["Проверка", "Результат", "Граница доказательства"], [
        ["Backend WebSocket нагрузка", "20 занятий 100 клиентов p95 4.88 мс", "demo no db текущая машина не браузер и не VoIP"],
        ["Fan out наблюдателям", "800 из 800 событий", "80 наблюдателей в локальном прогоне"],
        ["ИИ сценарии", "Строгая структура и серверная валидация", "Качество всех 96 сюжетов не оценено методистом"],
        ["Frontend", "TypeScript проверка и production build", "Не заменяет тест на реальных мобильных устройствах"],
        ["Отчёт", "CSV и PDF покрыты тестами", "PDF отчёта не является сертификатом"],
        ["Голос", "Whisper small не принят по скорости", "Нужен замер на целевом сервере"],
    ], [2.1, 2.05, 2.55])
    add_paragraph(document, "Машинно читаемый результат нагрузки хранится в docs/evidence/load-dds-2026-09-23.json. Для формальной приёмки требуется повторить прогон с PostgreSQL на сервере заказчика, измерить браузерную отрисовку, 100 операций записи в секунду, отчёт до 30 секунд и восстановление после 30 секунд сетевого сбоя.")

    add_section(document, "Ограничения и незакрытые требования")
    add_table(document, ["Требование", "Текущее состояние", "Что требуется"], [
        ["Локальный SIP сервер", "Отсутствует", "Развернуть SIP сервер подключить два софтфона и измерить задержку до 150 мс"],
        ["TLS внутри контура", "Не завершено", "Reverse proxy сертификаты принудительный HTTPS и WSS"],
        ["100 браузеров", "Проверен только backend", "Браузерный нагрузочный прогон на целевом стенде"],
        ["PostgreSQL 100 операций в секунду", "Не измерено", "Отдельный профиль записи и журналирования"],
        ["Восстановление сети 30 секунд", "Не подтверждено", "Буферизация повторное соединение и тест без потери событий"],
        ["Горизонтальное масштабирование", "Живое состояние в одном процессе", "Общий локальный слой состояния и маршрутизация сессий"],
        ["Мобильная версия", "Есть адаптивные правила", "Проверка реальных телефонов и исправление найденных дефектов"],
        ["XML и аудиозаписи", "Не реализованы", "Экспорт конфигураций и запись MP3 или WAV при включённом голосе"],
        ["Сертификат PDF", "Не реализован", "Шаблон сертификата и правила выдачи"],
        ["Приёмка Windows", "Инструкция подготовлена", "Холодный запуск и полный учебный маршрут на Windows"],
    ], [2.0, 2.05, 2.65])
    add_paragraph(document, "Эти пункты нельзя считать выполненными по наличию документации или имитации. Они остаются в реестре разрывов docs/spec/GAP.md до появления кода и приёмочных измерений.")

    add_section(document, "Библиотеки и лицензии")
    add_paragraph(document, "Ниже перечислены прямые зависимости приложения в текущем локальном окружении. Полный реестр установленных прямых и транзитивных пакетов с версиями и лицензиями сформирован в docs/delivery/THIRD-PARTY.md. Авторитетные версии зафиксированы в backend/uv.lock и frontend/package-lock.json. При поставке необходимо сохранить уведомления и тексты лицензий контейнеров, исполняемых файлов и весов моделей.")
    add_table(document, ["Компонент", "Версия", "Лицензия"], [
        ["FastAPI", "0.141.1", "MIT"], ["uvicorn", "0.53.0", "BSD 3 Clause"],
        ["Pydantic", "2.13.5", "MIT"], ["pydantic settings", "2.15.0", "MIT"],
        ["SQLAlchemy", "2.0.53", "MIT"], ["Alembic", "1.20.0", "MIT"],
        ["asyncpg", "0.31.0", "Apache 2.0"], ["httpx", "0.28.1", "BSD 3 Clause"],
        ["PyYAML", "6.0.3", "MIT"], ["ReportLab", "5.0.1", "BSD"],
        ["structlog", "26.1.0", "MIT or Apache 2.0"], ["ONNX Runtime", "1.30.0", "MIT"],
        ["tokenizers", "0.23.2", "Apache 2.0"], ["NumPy", "2.4.6", "BSD and bundled notices"],
        ["num2words", "0.5.14", "LGPL"], ["argon2 cffi", "25.1.0", "MIT"],
        ["itsdangerous", "2.2.0", "BSD"], ["websockets", "17.1", "BSD 3 Clause"],
        ["React", "18.3.1", "MIT"], ["React DOM", "18.3.1", "MIT"],
        ["React Router DOM", "6.30.6", "MIT"], ["TanStack React Query", "5.103.1", "MIT"],
        ["Vite", "5.4.21", "MIT"], ["TypeScript", "5.9.3", "Apache 2.0"],
        ["PostgreSQL", "16 alpine image", "PostgreSQL License"],
    ], [2.45, 1.45, 2.75])
    add_paragraph(document, "Локальные модели распространяются отдельно от исходного кода. Перед передачей весов нужно проверить условия конкретных репозиториев Qwen, Vikhr, whisper.cpp и Silero, а также сохранить их LICENSE и model card. Репозиторий не переопределяет лицензии третьих лиц.")

    add_section(document, "Приёмочный чек лист")
    add_table(document, ["Проверка", "Критерий"], [
        ["Установка", "Новый стенд запускается по инструкции без ручного изменения кода"],
        ["Вход", "Каждая роль видит только разрешённые экраны и данные"],
        ["Карточки", "Доступны 32 билета и 96 вызовов заказчика"],
        ["ДДС", "Карточка решение бригада связь доклады и статусы проходят полностью"],
        ["112", "Текстовая вводная создаёт карточку и передаёт её ДДС"],
        ["ИИ формирователь", "Черновик создаётся локально валидируется и появляется только после утверждения"],
        ["Оценка", "Критерии влияют на таймер и зачёт отчёт совпадает с журналом"],
        ["Экспорт", "CSV и PDF скачиваются и открываются"],
        ["Нагрузка", "Повторён прогон 20 занятий 100 клиентов и отдельный браузерный тест"],
        ["Надёжность", "Подтверждены БД 100 операций в секунду отчёт до 30 секунд и восстановление сети"],
        ["Безопасность", "DEV режим выключен секреты заменены TLS включён резервная копия восстановлена"],
        ["SIP", "Два локальных абонента регистрируются и звонят с задержкой до 150 мс"],
        ["Windows и мобильные", "Полный маршрут проверен на целевой Windows машине и реальном телефоне"],
    ], [2.2, 4.45])
    add_paragraph(document, "Результаты приёмки должны содержать дату, конфигурацию оборудования, версии образов и приложенные машинно читаемые логи. Пункты без такого свидетельства остаются неподтверждёнными.")

    add_section(document, "Связанные материалы")
    add_bullets(document, [
        "README.md — быстрый запуск и карта документации",
        "docs/spec/TZ.md и docs/spec/TZ-FULL.md — рабочая и полная спецификация",
        "docs/spec/GAP.md — текущий реестр соответствия и разрывов",
        "docs/arch/CONTRACT.md — HTTP и WebSocket контракт",
        "docs/arch/OFFLINE.md — перенос в изолированную сеть",
        "docs/arch/LOCAL-MODELS.md — Qwen Vikhr Whisper и Windows",
        "docs/arch/LOAD-TEST.md — нагрузочный сценарий и границы измерения",
        "docs/product/METHODOLOGY.md — метрики ошибки и структура занятия",
        "docs/delivery/THIRD-PARTY.md — полный реестр пакетов и лицензий текущего окружения",
    ])

    properties = document.core_properties
    properties.title = "Учебный симулятор работы оператора 112 и диспетчера ДДС"
    properties.subject = "Сопроводительная документация к решению задачи 9"
    properties.author = "Проект lct-hack"
    properties.keywords = "Система 112 ДДС обучение локальный контур"
    properties.comments = "Сформировано из документации репозитория 23 сентября 2026 года"

    document.save(OUT_DOCX)
    print(OUT_DOCX)
    return OUT_DOCX


if __name__ == "__main__":
    build()
