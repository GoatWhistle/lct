"""Экспорт разбора: содержимое, безопасность CSV, кириллица и доступ."""

import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.auth import Principal
from app.api.http import sessions
from app.domain.events import SessionReport
from app.domain.roles import Role
from app.main import app
from app.scoring.export import _cell, to_csv, to_pdf


def sample_report(*, long: bool = False) -> SessionReport:
    at = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    long_text = "Заявитель сообщает о дыме в учебном помещении. " * (240 if long else 1)
    return SessionReport.model_validate({
        "session_id": str(uuid4()),
        "scenario_id": "=1+1",
        "mode": "training",
        "attempt": 2,
        "transcript": [
            {"ref": f"u{i}", "speaker": "caller", "text": long_text if i == 0 else "<вопрос> \t=cmd", "at": at}
            for i in range(18 if long else 2)
        ],
        "findings": [{
            "code": "E1", "source": "slots", "summary": "+HYPERLINK(\"x\")",
            "fact": "Адрес не уточнён", "norm": "Уточнить адрес происшествия",
            "ref": "ГОСТ", "at": at,
        }],
        "metrics": [{
            "key": "address", "title": "Адрес", "fact": "Не назван",
            "norm": "Адрес должен быть уточнён", "passed": False,
        }],
        "competencies": [{"competency": "interview", "value": 65}],
        "reference_questions": [{"checklist_id": "q_address", "question": "Назовите адрес?"}],
        "missed_checklist": ["q_address"],
        "hints_used": [{"checklist_id": "q_address", "question": "Уточните адрес", "at": at}],
        "self_assessment": {"missed": ["q_address"], "comment": "@SUM(1,2)", "submitted_at": at},
        "self_assessment_diff": {"noticed": ["q_address"], "unnoticed": [], "overcautious": []},
        "notes": [{"transcript_ref": "u0", "text": "Внимательнее к адресу", "author": "Преподаватель"}],
        "score_auto": 70,
        "score_final": 75,
        "overridden_by": "Преподаватель",
        "override_comment": "Ручная корректировка",
    })


def test_csv_contains_sections_and_blocks_formula_injection():
    rows = list(csv.reader(io.StringIO(to_csv(sample_report()).decode("utf-8-sig"))))
    assert rows[0] == ["Раздел", "№", "Поле", "Значение", "Дополнительно"]
    assert any(row[0] == "Транскрипт" and row[3] == "<вопрос> \t=cmd" for row in rows)
    assert any(row[0] == "Занятие" and row[3] == "'=1+1" for row in rows)
    assert any(row[0] == "Ошибки" and row[3] == "'+HYPERLINK(\"x\")" for row in rows)
    assert any(row[0] == "Самооценка" and row[3] == "'@SUM(1,2)" for row in rows)
    assert _cell("  =cmd") == "'  =cmd"
    assert _cell("\tОбычный текст") == "'\tОбычный текст"


def test_pdf_supports_cyrillic_and_spans_pages(tmp_path):
    data = to_pdf(sample_report(long=True))
    assert data.startswith(b"%PDF-")
    path = tmp_path / "report.pdf"
    path.write_bytes(data)
    # Poppler даёт проверку извлекаемого текста, не только сигнатуры файла.
    import shutil
    import subprocess

    if shutil.which("pdftotext") and shutil.which("pdfinfo"):
        info = subprocess.check_output(["pdfinfo", str(path)], text=True)
        pages = int(next(line.split(":", 1)[1].strip() for line in info.splitlines() if line.startswith("Pages:")))
        assert pages >= 2
        extracted = subprocess.check_output(["pdftotext", str(path), "-"], text=True)
        assert "Отчёт по учебному занятию" in extracted
        assert "Адрес должен быть уточнён" in extracted
        assert "Заявитель сообщает о дыме" in extracted


@pytest.fixture
def client(monkeypatch):
    report = sample_report()
    state = SimpleNamespace(score={"score_auto": 70}, trainee_id=uuid4())
    monkeypatch.setattr(sessions, "_live", lambda session_id: (state, object()))
    monkeypatch.setattr(sessions, "build_report", lambda session_id, state, scenario: report)
    with TestClient(app) as test_client:
        test_client.post("/api/auth/dev-token")
        yield test_client, state, report


def test_export_routes_return_downloads_with_json_report_rights(client):
    browser, state, report = client
    csv_response = browser.get(f"/api/sessions/{report.session_id}/report.csv")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert csv_response.content.startswith(b"\xef\xbb\xbf")
    assert "attachment" in csv_response.headers["content-disposition"]

    pdf_response = browser.get(f"/api/sessions/{report.session_id}/report.pdf")
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert pdf_response.content.startswith(b"%PDF-")

    state.score = None
    assert browser.get(f"/api/sessions/{report.session_id}/report.csv").status_code == 409
    assert browser.get(f"/api/sessions/{report.session_id}/report.pdf").status_code == 409


def test_trainee_cannot_export_another_persons_report(client, monkeypatch):
    browser, state, report = client
    monkeypatch.setattr(
        sessions, "require",
        lambda request: Principal(login="trainee", full_name="Учебный", role=Role.TRAINEE, trainee_id=uuid4()),
    )
    for suffix in ("csv", "pdf"):
        assert browser.get(f"/api/sessions/{report.session_id}/report.{suffix}").status_code == 403
