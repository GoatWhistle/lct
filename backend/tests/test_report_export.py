"""Экспорт разбора: содержимое, безопасность CSV, кириллица и доступ."""

import asyncio
import csv
import io
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.session.pg_store import PostgresSessionStore
from app.session.store import MemorySessionStore
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.auth import Principal
from app.api.http import sessions
from app.config import get_settings
from app.domain.events import SessionReport
from app.domain.roles import Role
from app.main import app
from app.scoring.export import _cell, certificate_pdf, to_csv, to_pdf


def sample_report(*, long: bool = False) -> SessionReport:
    at = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    long_text = "Заявитель сообщает о дыме в учебном помещении. " * (240 if long else 1)
    return SessionReport.model_validate({
        "session_id": str(uuid4()),
        "scenario_id": "=1+1",
        "mode": "training",
        "attempt": 2,
        "criteria": {"decision_time_limit_seconds": 45, "allowed_errors": 1,
                     "require_correct_grammar": True},
        "failed_metrics": 1,
        "passed": True,
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
    assert ["Критерии", "", "Лимит реакции на доклад бригады, с", "не применяется", ""] in rows
    assert _cell("  =cmd") == "'  =cmd"
    assert _cell("\tОбычный текст") == "'\tОбычный текст"


def test_csv_shows_enabled_report_reaction_limit():
    report = sample_report()
    report.criteria.dds_report_reaction_enabled = True
    report.criteria.dds_report_reaction_limit_seconds = 20

    rows = list(csv.reader(io.StringIO(to_csv(report).decode("utf-8-sig"))))

    assert ["Критерии", "", "Лимит реакции на доклад бригады, с", "20", ""] in rows


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
        assert "Лимит реакции на доклад бригады" in extracted
        assert "не применяется" in extracted


def test_certificate_pdf_contains_saved_result(tmp_path):
    data = certificate_pdf(
        trainee_name="Петров Пётр Сергеевич",
        trainee_id=uuid4(),
        group_name="ДДС-17",
        attempts=4,
        average_score=87.25,
        issued_at="2026-09-23",
    )
    assert data.startswith(b"%PDF-")
    path = tmp_path / "certificate.pdf"
    path.write_bytes(data)
    import shutil
    import subprocess

    if shutil.which("pdftotext"):
        extracted = subprocess.check_output(["pdftotext", str(path), "-"], text=True)
        assert "СЕРТИФИКАТ" in extracted
        assert "Петров Пётр Сергеевич" in extracted
        assert "87.2 из 100" in extracted
        assert "не заменяет квалификационный документ" in extracted


@pytest.fixture
def client(monkeypatch):
    report = sample_report()
    owner_login = "demo-instructor" if get_settings().demo_no_db else "dev"
    state = SimpleNamespace(score={"score_auto": 70}, trainee_id=uuid4(), owner_login=owner_login)
    audit_events = []

    async def capture_audit(actor, role, action, object_id=None, detail=""):
        audit_events.append((actor, role, action, object_id))

    state.audit_events = audit_events
    monkeypatch.setattr(sessions, "_live", lambda session_id: (state, object()))
    monkeypatch.setattr(sessions, "build_report", lambda session_id, state, scenario: report)
    monkeypatch.setattr(sessions, "audit_required", capture_audit)
    with TestClient(app) as test_client:
        # These endpoint tests exercise the in-memory live-report path. Durable
        # report readiness is covered by the isolated PostgreSQL integration suite.
        monkeypatch.setattr(sessions.hub, "store", MemorySessionStore())
        test_client.post("/api/auth/dev-token")
        yield test_client, state, report


def test_export_routes_return_downloads_with_json_report_rights(client):
    browser, state, report = client
    json_response = browser.get(f"/api/sessions/{report.session_id}/report")
    assert json_response.status_code == 200, json_response.text

    csv_response = browser.get(f"/api/sessions/{report.session_id}/report.csv")
    assert csv_response.status_code == 200, csv_response.text
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert csv_response.content.startswith(b"\xef\xbb\xbf")
    assert "attachment" in csv_response.headers["content-disposition"]

    pdf_response = browser.get(f"/api/sessions/{report.session_id}/report.pdf")
    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert pdf_response.content.startswith(b"%PDF-")
    assert state.audit_events == [
        ("dev", "instructor", "report.read", str(report.session_id)),
        ("dev", "instructor", "report.export.csv", str(report.session_id)),
        ("dev", "instructor", "report.export.pdf", str(report.session_id)),
    ]

    state.score = None
    assert browser.get(f"/api/sessions/{report.session_id}/report.csv").status_code == 409
    assert browser.get(f"/api/sessions/{report.session_id}/report.pdf").status_code == 409


def test_trainee_cannot_export_another_persons_report(client, monkeypatch):
    browser, _state, report = client
    monkeypatch.setattr(
        sessions, "require",
        lambda request: Principal(login="trainee", full_name="Учебный", role=Role.TRAINEE, trainee_id=uuid4()),
    )
    for suffix in ("csv", "pdf"):
        assert browser.get(f"/api/sessions/{report.session_id}/report.{suffix}").status_code == 403
    assert not client[1].audit_events


def test_report_export_fails_closed_when_access_audit_is_unavailable(client, monkeypatch):
    from fastapi import HTTPException

    browser, _state, report = client

    async def unavailable(*_args, **_kwargs):
        raise HTTPException(status_code=503, detail="audit_unavailable")

    monkeypatch.setattr(sessions, "audit_required", unavailable)
    response = browser.get(f"/api/sessions/{report.session_id}/report.pdf")
    assert response.status_code == 503
    assert response.json() == {"detail": "audit_unavailable"}


def test_archived_report_survives_missing_live_session(monkeypatch):
    archived = sample_report()
    session_row = SimpleNamespace(id=archived.session_id, trainee_id=uuid4(), owner_login="teacher")
    score_row = SimpleNamespace(
        score_auto=70.0,
        score_final=82.0,
        overridden_by="Преподаватель",
        override_comment="проверено после занятия",
        report={"full_report": archived.model_dump(mode="json")},
    )

    class FakeDb:
        async def get(self, model, key):
            return session_row

        async def scalar(self, statement):
            return score_row

    monkeypatch.setattr(sessions, "_live", lambda session_id: (_ for _ in ()).throw(
        HTTPException(status_code=404, detail="session_not_found")
    ))
    monkeypatch.setattr(
        sessions, "require",
        lambda request: Principal(login="teacher", full_name="Преподаватель", role=Role.INSTRUCTOR),
    )
    restored = asyncio.run(sessions._report_data(archived.session_id, object(), FakeDb()))
    assert restored.session_id == archived.session_id
    assert restored.score_auto == 70
    assert restored.score_final == 82
    assert restored.override_comment == "проверено после занятия"


def test_archived_score_override_updates_json_exports_and_audit(monkeypatch):
    archived = sample_report()
    session_row = SimpleNamespace(id=archived.session_id, trainee_id=uuid4(), owner_login="teacher")
    score_row = SimpleNamespace(
        score_auto=70.0,
        score_final=70.0,
        overridden_by=None,
        override_comment=None,
        report={"full_report": archived.model_dump(mode="json")},
    )

    class FakeDb:
        def __init__(self):
            self.added = []
            self.commits = 0

        async def get(self, model, key):
            assert key == archived.session_id
            return session_row

        async def scalar(self, statement):
            return score_row

        def add(self, row):
            self.added.append(row)

        async def flush(self):
            pass

        async def rollback(self):
            pass

        async def commit(self):
            self.commits += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    db = FakeDb()
    monkeypatch.setattr(sessions.hub, "store", PostgresSessionStore(lambda: db))
    monkeypatch.setattr(sessions.hub, "get", lambda session_id: None)
    monkeypatch.setattr(sessions, "_live", lambda session_id: (_ for _ in ()).throw(
        HTTPException(status_code=404, detail="session_not_found")
    ))
    monkeypatch.setattr(
        sessions, "require",
        lambda request, *roles: Principal(login="teacher", full_name="Преподаватель", role=Role.INSTRUCTOR),
    )
    audit_events = []

    async def capture_audit(actor, role, action, object_id=None, detail=""):
        audit_events.append((actor, role, action, object_id))

    monkeypatch.setattr(sessions, "audit_required", capture_audit)

    corrected = asyncio.run(sessions.override(
        archived.session_id,
        sessions.ScoreOverride(score_final=84.5, comment="проверена запись переговоров"),
        object(),
        db,
    ))

    assert db.commits == 1, "оценка и аудит должны фиксироваться одной транзакцией"
    assert score_row.score_auto == 70.0
    assert score_row.score_final == 84.5
    assert score_row.overridden_by == "teacher"
    assert score_row.report["full_report"]["score_final"] == 84.5
    assert corrected.score_auto == 70.0 and corrected.score_final == 84.5
    assert corrected.override_comment == "проверена запись переговоров"
    audit = db.added[0]
    assert audit.action == "score.override" and audit.actor == "teacher"
    assert "84.5" in audit.detail and "comment_chars=" in audit.detail
    assert "проверена запись переговоров" not in audit.detail

    report = asyncio.run(sessions.report(archived.session_id, object(), db))
    assert report.score_final == 84.5 and report.score_auto == 70.0
    csv_response = asyncio.run(sessions.report_csv(archived.session_id, object(), db))
    assert "84.5" in csv_response.body.decode("utf-8-sig")
    assert "проверена запись переговоров" in csv_response.body.decode("utf-8-sig")
    pdf_response = asyncio.run(sessions.report_pdf(archived.session_id, object(), db))
    assert pdf_response.body.startswith(b"%PDF-")
    assert audit_events == [
        ("teacher", "instructor", "report.read", str(archived.session_id)),
        ("teacher", "instructor", "report.export.csv", str(archived.session_id)),
        ("teacher", "instructor", "report.export.pdf", str(archived.session_id)),
    ]
