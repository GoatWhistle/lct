"""Разбор по отметкам (lct-35): преподаватель подтверждает, снимает и добавляет отметку.

Балл пересчитывается теми же метриками и весами: снятая отметка не штрафует
свою метрику, ручная — штрафует. Автоматическая оценка остаётся рядом.
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.auth import Principal
from app.api.http import sessions
from app.domain.events import Metric
from app.domain.roles import Role
from app.domain.taxonomy import (
    ErrorCode,
    Finding,
    FindingDecision,
    FindingReview,
    FindingSource,
)
from app.main import app
from app.scoring.export import to_csv
from app.scoring.review import INSTRUCTOR_METRIC, add_finding, review_finding
from app.session import finish as finish_module
from app.session.hub import hub
from app.session.store import FindingAdded, FindingReviewed, MemorySessionStore

from tests.test_dds_desk_scores import dds_exercise_early_finish

AT = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def review(decision: FindingDecision) -> FindingReview:
    return FindingReview(decision=decision, reason="доклад пришёл по рации", author="teacher", at=AT)


def report_with(metrics: list[Metric], findings: list[Finding]) -> dict:
    return {
        "score_auto": 50.0,
        "metrics": [metric.model_dump(mode="json") for metric in metrics],
        "findings": [finding.model_dump(mode="json") for finding in findings],
        "full_report": {"criteria": {"allowed_errors": 0}, "score_auto": 50.0,
                        "score_final": 50.0, "failed_metrics": 1, "passed": False},
    }


def dds_metric(key: str, passed: bool, card: int = 1) -> Metric:
    return Metric(key=key, title=key, fact="факт", norm="норма", passed=passed,
                  weight=1.0, card=card, service="01")


def dds_finding(code: ErrorCode, card: int = 1) -> Finding:
    return Finding(code=code, source=FindingSource.DISPATCHER, summary=code.value,
                   fact="факт", norm="норма", card=card, service="01")


def test_dismissed_finding_stops_penalizing_its_metric_but_stays_in_report():
    report = report_with(
        [dds_metric("dds_primary", True), dds_metric("dds_progress", False)],
        [dds_finding(ErrorCode.D6)],
    )

    updated = review_finding(report, 0, review(FindingDecision.DISMISSED))

    assert updated["score_reviewed"] == 100.0
    assert updated["score_final"] == 100.0
    assert updated["score_auto"] == 50.0, "автооценка остаётся рядом"
    progress = updated["metrics"][1]
    assert (progress["passed"], progress["passed_auto"]) == (True, False)
    finding = updated["findings"][0]
    assert finding["review"]["decision"] == "dismissed"
    assert finding["review"]["reason"] == "доклад пришёл по рации"
    archived = updated["full_report"]
    assert (archived["score_final"], archived["failed_metrics"], archived["passed"]) == (100.0, 0, True)


def test_confirmed_finding_and_other_card_keep_the_metric_failed():
    report = report_with(
        [dds_metric("dds_progress", False, card=1), dds_metric("dds_progress", False, card=2)],
        [dds_finding(ErrorCode.D6, card=1), dds_finding(ErrorCode.D6, card=2)],
    )

    confirmed = review_finding(report, 0, review(FindingDecision.CONFIRMED))
    assert confirmed["score_reviewed"] == 0.0

    dismissed = review_finding(report, 0, review(FindingDecision.DISMISSED))
    assert [metric["passed"] for metric in dismissed["metrics"]] == [True, False], (
        "снятие отметки первой карточки не трогает вторую"
    )


def test_metric_stays_failed_while_one_of_its_findings_is_active():
    report = report_with(
        [dds_metric("dds_reply", False)],
        [dds_finding(ErrorCode.D5), dds_finding(ErrorCode.D5)],
    )

    updated = review_finding(report, 0, review(FindingDecision.DISMISSED))

    assert updated["metrics"][0]["passed"] is False
    again = review_finding(updated, 1, review(FindingDecision.DISMISSED))
    assert again["metrics"][0]["passed"] is True


def test_instructor_finding_penalizes_linked_metric_and_unlinked_code_adds_one():
    report = report_with([dds_metric("dds_progress", True), dds_metric("dds_primary", True)], [])
    manual = Finding(code=ErrorCode.D6, source=FindingSource.INSTRUCTOR, summary="Карточка 1: D6",
                     fact="«Прибыла» отмечено до доклада", norm="по факту доклада",
                     card=1, service="01", author="teacher", at=AT)

    updated = add_finding(report, manual)

    assert updated["score_reviewed"] == 50.0
    assert updated["metrics"][0]["passed"] is False
    assert updated["metrics"][0]["passed_auto"] is True
    assert updated["findings"][0]["source"] == "instructor"

    # У D4 в методике нет метрики — ручная отметка всё равно должна штрафовать.
    no_metric = manual.model_copy(update={"code": ErrorCode.D4})
    extra = add_finding(report, no_metric)
    assert [metric["key"] for metric in extra["metrics"]][-1] == INSTRUCTOR_METRIC
    assert extra["score_reviewed"] == pytest.approx(66.7)

    # Снятая ручная отметка не штрафует, и её метрика исчезает при пересчёте.
    lifted = review_finding(extra, 0, review(FindingDecision.DISMISSED))
    assert lifted["score_reviewed"] == 100.0
    assert INSTRUCTOR_METRIC not in {metric["key"] for metric in lifted["metrics"]}


def test_score_override_stays_final_after_finding_review():
    report = {**report_with([dds_metric("dds_progress", False)], [dds_finding(ErrorCode.D6)]),
              "score_final": 70.0, "overridden_by": "teacher"}

    updated = review_finding(report, 0, review(FindingDecision.DISMISSED))

    assert updated["score_reviewed"] == 100.0
    assert updated["score_final"] == 70.0


def test_unknown_finding_index_is_refused():
    with pytest.raises(LookupError):
        review_finding(report_with([], []), 0, review(FindingDecision.DISMISSED))


# ── HTTP: живое занятие ДДС ──

@pytest.fixture
def lesson(monkeypatch):
    """Завершённое занятие ДДС преподавателя `dev` в памяти узла."""

    class NoCoaching:
        def model_dump(self, **_kwargs):
            return {}

    async def no_coach(_metrics):
        return NoCoaching()

    async def audit_in_memory(*_args, **_kwargs):
        return None

    async def no_database():
        yield None

    # Живое занятие в памяти: чтение отчёта не должно ходить в PostgreSQL.
    monkeypatch.setattr(finish_module, "coach", no_coach)
    monkeypatch.setattr(sessions, "audit_required", audit_in_memory)
    monkeypatch.setitem(app.dependency_overrides, sessions.optional_session, no_database)
    store = MemorySessionStore()
    monkeypatch.setattr(hub, "store", store)
    state = dds_exercise_early_finish()
    state.owner_login = "dev"
    with TestClient(app) as browser:
        browser.post("/api/auth/dev-token")
        hub.store = store
        hub.register(state)
        yield browser, state, store


def as_role(monkeypatch, role: Role, login: str = "dev") -> None:
    def require(_request, *roles):
        if roles and role not in roles:
            raise HTTPException(status_code=403, detail="forbidden")
        return Principal(login=login, full_name="Проверка", role=role,
                         trainee_id=uuid4() if role is Role.TRAINEE else None)

    monkeypatch.setattr(sessions, "require", require)


def test_owner_dismisses_and_adds_finding_through_http(lesson):
    browser, state, store = lesson
    session_id = state.session_id
    before = browser.get(f"/api/sessions/{session_id}/report").json()
    target = next(index for index, finding in enumerate(before["findings"])
                  if finding["code"] == "D1")

    dismissed = browser.post(
        f"/api/sessions/{session_id}/findings/{target}/review",
        json={"decision": "dismissed", "reason": "карточку выдали с опозданием"},
    )
    assert dismissed.status_code == 200, dismissed.text
    body = dismissed.json()
    assert body["findings"][target]["review"]["decision"] == "dismissed"
    assert body["findings"][target]["review"]["author"] == "dev"
    assert body["score_auto"] == before["score_auto"]
    assert body["score_reviewed"] >= before["score_auto"]

    added = browser.post(
        f"/api/sessions/{session_id}/findings",
        json={"code": "D5", "fact": "в комментарии нет адресата", "norm": "назвать адресата",
              "card": 1},
    )
    assert added.status_code == 200, added.text
    manual = added.json()["findings"][-1]
    assert (manual["source"], manual["card"], manual["author"]) == ("instructor", 1, "dev")
    assert manual["summary"].startswith("Карточка 1: ")

    records = [record for _, batch in store.commits for record in batch]
    assert [type(record) for record in records[-2:]] == [FindingReviewed, FindingAdded]
    assert store.scores[session_id]["findings"][target]["review"]["decision"] == "dismissed"

    rows = to_csv(sessions.SessionReport.model_validate(added.json())).decode("utf-8-sig")
    assert "карточку выдали с опозданием" in rows
    assert "в комментарии нет адресата" in rows


def test_review_requires_reason_and_existing_card(lesson):
    browser, state, _store = lesson
    session_id = state.session_id

    blank = browser.post(f"/api/sessions/{session_id}/findings/0/review",
                         json={"decision": "dismissed", "reason": "   "})
    assert blank.status_code == 422
    missing = browser.post(f"/api/sessions/{session_id}/findings/999/review",
                           json={"decision": "confirmed", "reason": "проверено"})
    assert missing.status_code == 404
    card = browser.post(f"/api/sessions/{session_id}/findings",
                        json={"code": "D1", "fact": "факт", "norm": "норма", "card": 99})
    assert card.status_code == 422


@pytest.mark.parametrize("role, login, status", [
    (Role.TRAINEE, "trainee", 403),
    (Role.INSTRUCTOR, "someone-else", 404),
])
def test_trainee_and_foreign_instructor_cannot_review(lesson, monkeypatch, role, login, status):
    browser, state, store = lesson
    commits = len(store.commits)
    as_role(monkeypatch, role, login)

    reviewed = browser.post(f"/api/sessions/{state.session_id}/findings/0/review",
                            json={"decision": "dismissed", "reason": "не согласен"})
    added = browser.post(f"/api/sessions/{state.session_id}/findings",
                         json={"code": "D1", "fact": "факт", "norm": "норма"})

    assert (reviewed.status_code, added.status_code) == (status, status)
    assert len(store.commits) == commits, "отказ ничего не пишет"


def test_finding_review_is_audited_in_postgres(postgres_access):
    """Решение и аудит — одна транзакция: строка Score и AuditLog вместе."""
    from sqlalchemy import select

    from app.db.base import get_sessionmaker
    from app.db.models import AuditLog, Score
    from app.session.pg_store import PostgresSessionStore
    from app.session.store import LessonRequest

    store = PostgresSessionStore(get_sessionmaker())
    session_id = uuid4()
    report = report_with([dds_metric("dds_progress", False)], [dds_finding(ErrorCode.D6)])

    async def scenario():
        from app.session.state import SessionState
        from app.domain.events import Exercise, SessionMode

        def build(_identity):
            return SessionState(session_id=session_id, scenario_id="fire-apartment-l2",
                                scenario_title="Пожар", level="L2", mode=SessionMode.TRAINING,
                                exercise=Exercise.DDS)

        await store.open(LessonRequest(
            session_id=session_id, scenario_id="fire-apartment-l2", mode="training",
            trainee_name=None, trainee_id=None, owner_login="teacher", backend_node_id=None,
        ), build)
        from app.session.store import ScoreCalculated
        await store.commit_archived(session_id, [ScoreCalculated(50.0, report)])
        await store.commit_archived(session_id, [FindingReviewed(
            index=0, role="instructor", review=review(FindingDecision.DISMISSED),
        )])
        async with get_sessionmaker()() as db:
            score = await db.scalar(select(Score).where(Score.session_id == session_id))
            audit = await db.scalar(select(AuditLog).where(
                AuditLog.object_id == str(session_id), AuditLog.action == "finding.review",
            ))
            return score, audit

    score, audit = asyncio.run(scenario())
    assert score.score_auto == 50.0
    assert score.score_final == 100.0
    assert score.report["findings"][0]["review"]["decision"] == "dismissed"
    assert audit is not None and audit.actor == "teacher"
    assert "dismissed" in audit.detail and "50.0 → 100.0" in audit.detail
