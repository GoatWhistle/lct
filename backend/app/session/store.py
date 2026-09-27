"""Port хранилища живого занятия.

Операция курсанта или преподавателя фиксируется одним `commit`: проверка
владения `(node_id, epoch)`, снимок состояния и append-only строки — в одной
транзакции. Хаб публикует события только после успешного `commit`; сбой
отбрасывает их и закрывает занятие на этом узле (docs/arch/SCALE-OUT.md).

Adapter-ы: PostgreSQL (`app.session.pg_store`) и in-memory — для `DEMO_NO_DB`
и юнит-тестов.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.events import TranscriptEntry
from app.domain.taxonomy import Finding, FindingReview
from app.scoring.review import add_finding, review_finding
from app.session.checkpoint import dump_state
from app.session.state import SessionState


class SessionLeaseLost(RuntimeError):
    """This process no longer owns the durable session generation."""


# ── append-only строки операции ──

@dataclass(frozen=True)
class UtteranceAppended:
    entry: TranscriptEntry


@dataclass(frozen=True)
class HintRecorded:
    checklist_id: str
    question: str
    at: datetime


@dataclass(frozen=True)
class NoteAdded:
    transcript_ref: str
    text: str
    author: str


@dataclass(frozen=True)
class SelfAssessed:
    missed: list[str]
    comment: str
    at: datetime


@dataclass(frozen=True)
class LessonStarted:
    at: datetime


@dataclass(frozen=True)
class LessonEnded:
    at: datetime
    reason: str


@dataclass(frozen=True)
class ScoreCalculated:
    score_auto: float
    report: dict


@dataclass(frozen=True)
class ScoreArchived:
    """Полный разбор дополнен после оценки — например, самооценкой курсанта."""
    report: dict


@dataclass(frozen=True)
class ScoreOverridden:
    score_final: float
    author: str
    role: str
    comment: str


@dataclass(frozen=True)
class FindingReviewed:
    """Преподаватель подтвердил или снял отметку разбора."""
    index: int
    review: FindingReview
    role: str


@dataclass(frozen=True)
class FindingAdded:
    """Преподаватель добавил свою отметку (`source=instructor`)."""
    finding: Finding
    role: str


Record = (UtteranceAppended | HintRecorded | NoteAdded | SelfAssessed | LessonStarted
          | LessonEnded | ScoreCalculated | ScoreArchived | ScoreOverridden
          | FindingReviewed | FindingAdded)


def apply_finding_change(report: dict, change: "FindingReviewed | FindingAdded") -> dict:
    """Решение по отметке одинаково для живой сессии, строки `Score` и архива."""
    if isinstance(change, FindingReviewed):
        return review_finding(report, change.index, change.review)
    return add_finding(report, change.finding)


def apply_score_override(report: dict, override: ScoreOverridden) -> dict:
    """Правка балла одинакова для живой сессии, строки `Score` и архива.

    Автооценка остаётся рядом: видно, что скорректировано и кем. Архивный
    `full_report` правится тем же образом, иначе PDF и история покажут
    старую оценку.
    """
    fields = {
        "score_final": override.score_final,
        "overridden_by": override.author,
        "override_comment": override.comment,
    }
    updated = {**report, **fields}
    archived = report.get("full_report")
    if isinstance(archived, dict):
        updated["full_report"] = {
            **archived,
            "score_auto": report.get("score_auto", archived.get("score_auto")),
            **fields,
        }
    return updated


@dataclass(frozen=True)
class LessonIdentity:
    """То, что о занятии знает только хранилище: номер попытки, курсант, эпоха."""
    attempt: int
    trainee_id: UUID | None
    service: str | None
    fencing_epoch: int


@dataclass(frozen=True)
class LessonRequest:
    session_id: UUID
    scenario_id: str
    mode: str
    trainee_name: str | None
    trainee_id: UUID | None
    owner_login: str | None
    backend_node_id: str | None


class SessionStore(Protocol):
    #: Переживает ли занятие перезапуск процесса. Без этого каталог сценариев
    #: и отчёты берутся только из памяти.
    persistent: bool

    async def open(
        self, request: LessonRequest, build: Callable[[LessonIdentity], SessionState],
    ) -> SessionState:
        """Завести занятие, аудит запуска и первый снимок одной транзакцией."""
        ...

    async def commit(self, state: SessionState, records: Sequence[Record] = ()) -> None:
        """Проверка владения, снимок и строки — одна транзакция. Ошибка — исключение."""
        ...

    async def commit_archived(self, session_id: UUID, records: Sequence[Record]) -> None:
        """Строки к занятию, которого уже нет в памяти узла (правка балла из истории)."""
        ...

    async def restore_active(self) -> list[SessionState]: ...
    async def renew(self, session_id: UUID) -> None: ...
    async def claim_expired(self, session_id: UUID | None = None) -> list[SessionState]: ...


class MemorySessionStore:
    """Занятие живёт до перезапуска процесса: `DEMO_NO_DB` и юнит-тесты.

    Хранит то же, что PostgreSQL, в словарях — тесты читают снимок, строки
    и оценку тем же путём, что и сквозной сценарий.
    """

    persistent = False

    def __init__(self) -> None:
        self.commits: list[tuple[UUID, list[Record]]] = []
        self.records: dict[UUID, list[Record]] = {}
        self.scores: dict[UUID, dict] = {}
        self._snapshots: dict[UUID, dict | None] = {}

    async def open(
        self, request: LessonRequest, build: Callable[[LessonIdentity], SessionState],
    ) -> SessionState:
        state = build(LessonIdentity(
            attempt=1, trainee_id=request.trainee_id, service=None, fencing_epoch=0,
        ))
        self.commits.append((request.session_id, []))
        self._snapshots[request.session_id] = dump_state(state)
        return state

    async def commit(self, state: SessionState, records: Sequence[Record] = ()) -> None:
        records = list(records)
        self.commits.append((state.session_id, records))
        self._apply(state.session_id, records)
        self._snapshots[state.session_id] = None if state.ended else dump_state(state)

    async def commit_archived(self, session_id: UUID, records: Sequence[Record]) -> None:
        records = list(records)
        self.commits.append((session_id, records))
        self._apply(session_id, records)

    def _apply(self, session_id: UUID, records: list[Record]) -> None:
        for record in records:
            if isinstance(record, ScoreCalculated):
                self.scores[session_id] = dict(record.report)
            elif isinstance(record, ScoreArchived):
                self.scores[session_id] = dict(record.report)
            elif isinstance(record, ScoreOverridden):
                if session_id not in self.scores:
                    raise LookupError(f"нет оценки занятия {session_id}")
                self.scores[session_id] = apply_score_override(self.scores[session_id], record)
            elif isinstance(record, FindingReviewed | FindingAdded):
                if session_id not in self.scores:
                    raise LookupError(f"нет оценки занятия {session_id}")
                self.scores[session_id] = apply_finding_change(self.scores[session_id], record)
        self.records.setdefault(session_id, []).extend(records)

    def snapshot(self, session_id: UUID) -> dict | None:
        return self._snapshots.get(session_id)

    async def restore_active(self) -> list[SessionState]:
        return []

    async def renew(self, session_id: UUID) -> None:
        return None

    async def claim_expired(self, session_id: UUID | None = None) -> list[SessionState]:
        return []
