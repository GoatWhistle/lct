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
    assert finding["reviews"][-1]["decision"] == "dismissed"
    assert finding["reviews"][-1]["reason"] == "доклад пришёл по рации"
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
    assert body["findings"][target]["reviews"][-1]["decision"] == "dismissed"
    assert body["findings"][target]["reviews"][-1]["author"] == "dev"
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
    assert store.scores[session_id]["findings"][target]["reviews"][-1]["decision"] == "dismissed"

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
    assert score.report["findings"][0]["reviews"][-1]["decision"] == "dismissed"
    assert audit is not None and audit.actor == "teacher"
    assert "dismissed" in audit.detail and "50.0 → 100.0" in audit.detail


# ── История решений, коды, карточки, веса ──

def test_repeated_decision_keeps_history_and_last_one_applies():
    report = report_with([dds_metric("dds_progress", False)], [dds_finding(ErrorCode.D6)])

    dismissed = review_finding(report, 0, review(FindingDecision.DISMISSED))
    confirmed = review_finding(dismissed, 0, FindingReview(
        decision=FindingDecision.CONFIRMED, reason="перепроверил запись", author="head", at=AT,
    ))

    reviews = confirmed["findings"][0]["reviews"]
    assert [(item["decision"], item["author"], item["reason"]) for item in reviews] == [
        ("dismissed", "teacher", "доклад пришёл по рации"),
        ("confirmed", "head", "перепроверил запись"),
    ]
    assert confirmed["score_reviewed"] == 0.0, "действует последнее решение"


def test_report_saved_with_single_review_reads_as_history():
    legacy = dds_finding(ErrorCode.D6).model_dump(mode="json")
    legacy["review"] = review(FindingDecision.DISMISSED).model_dump(mode="json")

    finding = Finding.model_validate(legacy)

    assert [item.decision for item in finding.reviews] == [FindingDecision.DISMISSED]
    assert finding.review.decision is FindingDecision.DISMISSED


def test_call_code_in_handoff_penalizes_call_metric_not_instructor_metric():
    """Связка 112 → ДДС: E1 без карточки проваливает метрику 112, а не добавляет свою."""
    call = Metric(key="checklist_completeness", title="Полнота опроса", fact="все", norm="все",
                  passed=True, weight=1.0)
    report = report_with([call, dds_metric("dds_progress", True)], [])
    manual = Finding(code=ErrorCode.E1, source=FindingSource.INSTRUCTOR, summary="E1",
                     fact="не уточнён подъезд", norm="уточнить подъезд", author="teacher", at=AT)

    updated = add_finding(report, manual)

    assert [metric["passed"] for metric in updated["metrics"]] == [False, True]
    assert INSTRUCTOR_METRIC not in {metric["key"] for metric in updated["metrics"]}
    assert updated["score_reviewed"] == 50.0


def test_codes_and_card_scores_follow_the_review():
    card = {"card_id": str(uuid4()), "scenario_id": "s", "score_auto": 50.0, "reply_text": "",
            "metrics": [dds_metric("dds_primary", True).model_dump(mode="json"),
                        dds_metric("dds_progress", False).model_dump(mode="json")],
            "findings": [dds_finding(ErrorCode.D6).model_dump(mode="json")]}
    report = {**report_with([dds_metric("dds_primary", True), dds_metric("dds_progress", False)],
                            [dds_finding(ErrorCode.D6)]),
              "summary": {"hints": 2, "codes": {"D6": 1}}, "card_results": [card]}
    report["full_report"]["card_results"] = [card]

    dismissed = review_finding(report, 0, review(FindingDecision.DISMISSED))

    assert dismissed["summary"] == {"hints": 2, "codes": {}}, "снятая отметка не ошибка группы"
    for cards in (dismissed["card_results"], dismissed["full_report"]["card_results"]):
        assert cards[0]["score_auto"] == 50.0
        assert cards[0]["score_reviewed"] == 100.0
        assert cards[0]["metrics"][1]["passed"] is True
        assert cards[0]["findings"][0]["reviews"][-1]["decision"] == "dismissed"

    manual = Finding(code=ErrorCode.D4, source=FindingSource.INSTRUCTOR, summary="D4",
                     fact="отказ без причины", norm="причина обязательна", card=1,
                     author="teacher", at=AT)
    added = add_finding(dismissed, manual)
    assert added["summary"]["codes"] == {"D4": 1}
    assert added["card_results"][0]["score_reviewed"] == pytest.approx(66.7)


def test_instructor_metric_weight_comes_from_lesson_weights():
    report = report_with([dds_metric("dds_primary", True)], [])
    report["full_report"]["criteria"]["score_weights"] = {INSTRUCTOR_METRIC: 3.0}
    manual = Finding(code=ErrorCode.D4, source=FindingSource.INSTRUCTOR, summary="D4",
                     fact="отказ без причины", norm="причина обязательна", card=1,
                     author="teacher", at=AT)

    updated = add_finding(report, manual)

    assert updated["metrics"][-1]["weight"] == 3.0
    assert updated["score_reviewed"] == 25.0


def test_lesson_criteria_accept_instructor_metric_weight():
    from app.domain.events import LessonCriteria

    assert LessonCriteria(score_weights={INSTRUCTOR_METRIC: 2.0}).score_weights == {
        INSTRUCTOR_METRIC: 2.0}


def test_repeated_manual_finding_with_same_key_is_added_once():
    report = report_with([dds_metric("dds_primary", True)], [])
    manual = Finding(code=ErrorCode.D4, source=FindingSource.INSTRUCTOR, summary="D4",
                     fact="отказ без причины", norm="причина обязательна", card=1,
                     author="teacher", at=AT, client_id=uuid4())

    once = add_finding(report, manual)
    twice = add_finding(once, manual)

    assert len(twice["findings"]) == 1
    assert twice["score_reviewed"] == once["score_reviewed"] == 50.0


def test_pdf_shows_score_after_review(monkeypatch):
    from reportlab import platypus

    from app.scoring.export import to_pdf
    from tests.test_report_export import sample_report

    texts: list[str] = []

    class Recorded(platypus.Paragraph):
        def __init__(self, text, *args, **kwargs):
            texts.append(text)
            super().__init__(text, *args, **kwargs)

    # Абзацы перехватываются до вёрстки: извлечь текст из PDF нечем без poppler.
    monkeypatch.setattr(platypus, "Paragraph", Recorded)
    assert to_pdf(sample_report().model_copy(update={"score_reviewed": 91.5})).startswith(b"%PDF-")

    assert "<b>После разбора отметок:</b> 91.5" in texts


# ── HTTP: связка 112 → ДДС, идемпотентность, занятие из истории ──

def test_call_code_with_queue_card_is_refused_in_handoff(lesson):
    browser, state, store = lesson
    commits = len(store.commits)
    state.score["full_report"]["exercise"] = "call"

    refused = browser.post(f"/api/sessions/{state.session_id}/findings",
                           json={"code": "E1", "fact": "факт", "norm": "норма", "card": 1})

    assert refused.status_code == 422
    assert refused.json()["detail"] == "call_code_without_card"
    assert len(store.commits) == commits


def test_repeated_add_request_does_not_add_second_finding(lesson):
    browser, state, store = lesson
    body = {"code": "D5", "fact": "нет адресата", "norm": "назвать адресата", "card": 1,
            "client_id": str(uuid4())}

    first = browser.post(f"/api/sessions/{state.session_id}/findings", json=body)
    commits = len(store.commits)
    second = browser.post(f"/api/sessions/{state.session_id}/findings", json=body)

    assert first.status_code == second.status_code == 200
    assert len(second.json()["findings"]) == len(first.json()["findings"])
    assert second.json()["score_reviewed"] == first.json()["score_reviewed"]
    assert not [record for _, batch in store.commits[commits:] for record in batch], (
        "повтор ничего не пишет")


def test_archived_lesson_is_reviewed_through_http(monkeypatch):
    """Занятия нет в памяти узла: решение идёт через commit_archived, ответ — по строке после записи."""
    from types import SimpleNamespace

    from app.session.pg_store import PostgresSessionStore
    from tests.test_report_export import sample_report

    archived = sample_report().model_dump(mode="json")
    archived["findings"] = [dds_finding(ErrorCode.D6).model_dump(mode="json")]
    archived["metrics"] = [dds_metric("dds_progress", False).model_dump(mode="json")]
    archived.update(score_final=0.0, overridden_by=None, override_comment=None, score_auto=0.0)
    session_id = archived["session_id"]
    session_row = SimpleNamespace(id=session_id, trainee_id=uuid4(), owner_login="dev")
    score_row = SimpleNamespace(
        session_id=session_id, score_auto=0.0, score_final=0.0,
        report={"score_auto": 0.0, "summary": {"codes": {"D6": 1}},
                "findings": archived["findings"], "metrics": archived["metrics"],
                "full_report": archived},
    )

    class FakeDb:
        def __init__(self):
            self.added = []
            self.commits = 0

        async def get(self, _model, _key):
            return session_row

        async def scalar(self, _statement):
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

    async def fake_session():
        yield db

    monkeypatch.setattr(hub, "store", PostgresSessionStore(lambda: db))
    monkeypatch.setattr(hub, "get", lambda _session_id: None)
    monkeypatch.setitem(app.dependency_overrides, sessions.optional_session, fake_session)
    with TestClient(app) as browser:
        browser.post("/api/auth/dev-token")
        hub.store = PostgresSessionStore(lambda: db)
        response = browser.post(f"/api/sessions/{session_id}/findings/0/review",
                                json={"decision": "dismissed", "reason": "доклад по рации"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["score_reviewed"] == 100.0 and body["score_final"] == 100.0
    assert body["findings"][0]["reviews"][-1]["reason"] == "доклад по рации"
    assert db.commits == 1, "решение и аудит — один commit"
    assert score_row.score_final == 100.0
    assert score_row.report["summary"]["codes"] == {}
    audit = db.added[0]
    assert audit.action == "finding.review" and audit.actor == "dev"
    assert "D6 dismissed" in audit.detail and "доклад по рации" in audit.detail


def test_finding_review_is_not_saved_when_audit_fails(postgres_access):
    """Fail-closed: строка аудита не записалась — решения и нового балла тоже нет."""
    from sqlalchemy import select

    from app.db.base import get_sessionmaker
    from app.db.models import AuditLog, Score
    from app.domain.events import Exercise, SessionMode
    from app.session.pg_store import PostgresSessionStore
    from app.session.state import SessionState
    from app.session.store import LessonRequest, ScoreCalculated

    store = PostgresSessionStore(get_sessionmaker())
    session_id = uuid4()
    report = report_with([dds_metric("dds_progress", False)], [dds_finding(ErrorCode.D6)])
    # actor в audit_log — String(80): длинный логин роняет INSERT аудита
    # на стороне PostgreSQL, а Score меняется в той же транзакции.
    broken = FindingReview(decision=FindingDecision.DISMISSED, reason="доклад по рации",
                           author="x" * 200, at=AT)

    async def scenario():
        def build(_identity):
            return SessionState(session_id=session_id, scenario_id="fire-apartment-l2",
                                scenario_title="Пожар", level="L2", mode=SessionMode.TRAINING,
                                exercise=Exercise.DDS)

        await store.open(LessonRequest(
            session_id=session_id, scenario_id="fire-apartment-l2", mode="training",
            trainee_name=None, trainee_id=None, owner_login="teacher", backend_node_id=None,
        ), build)
        await store.commit_archived(session_id, [ScoreCalculated(50.0, report)])
        with pytest.raises(Exception):
            await store.commit_archived(session_id, [FindingReviewed(
                index=0, role="instructor", review=broken,
            )])
        async with get_sessionmaker()() as db:
            score = await db.scalar(select(Score).where(Score.session_id == session_id))
            audit = await db.scalar(select(AuditLog).where(
                AuditLog.object_id == str(session_id), AuditLog.action == "finding.review",
            ))
            return score, audit

    score, audit = asyncio.run(scenario())
    assert audit is None
    assert score.score_final == 50.0
    assert score.report["findings"][0].get("reviews", []) == []
    assert "score_reviewed" not in score.report


def test_archived_lesson_review_through_http_in_postgres(postgres_access, monkeypatch):
    """Тот же маршрут на настоящей PostgreSQL: ответ перечитан после commit под блокировкой."""
    from sqlalchemy import select

    from app.db.base import get_sessionmaker
    from app.db.models import AuditLog
    from app.domain.events import Exercise, SessionMode
    from app.session.pg_store import PostgresSessionStore
    from app.session.state import SessionState
    from app.session.store import LessonRequest, ScoreCalculated
    from tests.test_report_export import sample_report

    session_id = uuid4()
    archived = sample_report().model_dump(mode="json")
    archived.update(session_id=str(session_id), score_final=0.0, score_auto=0.0,
                    overridden_by=None, override_comment=None,
                    findings=[dds_finding(ErrorCode.D6).model_dump(mode="json")],
                    metrics=[dds_metric("dds_progress", False).model_dump(mode="json")])
    report = {"score_auto": 0.0, "summary": {"codes": {"D6": 1}},
              "findings": archived["findings"], "metrics": archived["metrics"],
              "full_report": archived}
    store = PostgresSessionStore(get_sessionmaker())

    async def prepare():
        def build(_identity):
            return SessionState(session_id=session_id, scenario_id="fire-apartment-l2",
                                scenario_title="Пожар", level="L2", mode=SessionMode.TRAINING,
                                exercise=Exercise.DDS)

        await store.open(LessonRequest(
            session_id=session_id, scenario_id="fire-apartment-l2", mode="training",
            trainee_name=None, trainee_id=None, owner_login="dev", backend_node_id=None,
        ), build)
        await store.commit_archived(session_id, [ScoreCalculated(0.0, report)])

    async def audit():
        async with get_sessionmaker()() as db:
            return await db.scalar(select(AuditLog).where(
                AuditLog.object_id == str(session_id), AuditLog.action == "finding.review"))

    monkeypatch.setattr(hub, "get", lambda _session_id: None)
    # Пул соединений привязан к циклу событий: подготовка и проверка идут
    # в цикле того же TestClient, что и запрос.
    with TestClient(app) as browser:
        browser.post("/api/auth/dev-token")
        previous, hub.store = hub.store, store
        try:
            browser.portal.call(prepare)
            response = browser.post(f"/api/sessions/{session_id}/findings/0/review",
                                    json={"decision": "dismissed", "reason": "доклад по рации"})
            row = browser.portal.call(audit)
        finally:
            hub.store = previous

    assert response.status_code == 200, response.text
    assert response.json()["score_final"] == 100.0
    assert response.json()["findings"][0]["reviews"][-1]["reason"] == "доклад по рации"
    assert row is not None and "доклад по рации" in row.detail
