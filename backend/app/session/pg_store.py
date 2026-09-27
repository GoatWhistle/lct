"""PostgreSQL adapter хранилища занятия.

Профиль курсанта, дельта попыток и аналитика группы строятся по этим строкам,
а не по памяти процесса: всё, что здесь не записано, для оценки не существует.
Каждый `commit` — одна транзакция: продление lease с проверкой `(node_id, epoch)`,
append-only строки операции и снимок `live_state`.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import repo
from app.db.models import (
    AuditLog,
    HintUse,
    InstructorNote,
    Score,
    SelfAssessment,
    Session,
    User,
    Utterance,
)
from app.domain.events import Mood, Speaker, TranscriptEntry
from app.session.checkpoint import dump_state, load_state
from app.session.state import SessionState, now_utc
from app.session.store import (
    HintRecorded,
    LessonEnded,
    LessonIdentity,
    LessonPaused,
    LessonRequest,
    LessonResumed,
    LessonStarted,
    NoteAdded,
    Record,
    ScoreArchived,
    ScoreCalculated,
    ScoreOverridden,
    SelfAssessed,
    SessionLeaseLost,
    UtteranceAppended,
    apply_score_override,
)

log = logging.getLogger(__name__)
LEASE_SECONDS = 15

__all__ = ["LEASE_SECONDS", "PostgresSessionStore", "SessionLeaseLost"]


class PostgresSessionStore:
    persistent = True

    def __init__(self, sessionmaker: async_sessionmaker, node_id: str | None = None) -> None:
        self._sessionmaker = sessionmaker
        self._node_id = node_id
        self._epochs: dict[UUID, int] = {}

    async def _fence(self, db, session_id: UUID, expected_epoch: int | None = None) -> None:
        """Renew and fence this write in the same transaction as its mutation."""
        if self._node_id is None:
            return
        epoch = expected_epoch if expected_epoch is not None else self._epochs.get(session_id)
        if epoch is None:
            raise SessionLeaseLost(f"session {session_id} has no local fencing epoch")
        now = now_utc()
        result = await db.execute(
            update(Session)
            .where(
                Session.id == session_id,
                Session.backend_node_id == self._node_id,
                Session.backend_fencing_epoch == epoch,
            )
            .values(backend_lease_until=now + timedelta(seconds=LEASE_SECONDS))
            .returning(Session.id)
        )
        if result.scalar_one_or_none() is None:
            raise SessionLeaseLost(f"session {session_id} owner epoch {epoch} was fenced")

    async def open(
        self, request: LessonRequest, build: Callable[[LessonIdentity], SessionState],
    ) -> SessionState:
        """Строка занятия, аудит запуска и первый снимок — одна транзакция.

        При сбое занятие не запускается без долговечной истории.
        """
        async with self._sessionmaker() as db:
            row = await repo.ensure_session(
                db,
                session_id=request.session_id,
                scenario_id=request.scenario_id,
                mode=request.mode,
                trainee_name=request.trainee_name,
                trainee_id=request.trainee_id,
                owner_login=request.owner_login,
                backend_node_id=request.backend_node_id or self._node_id,
                commit=False,
            )
            if row.backend_fencing_epoch <= 0:
                row.backend_fencing_epoch = 1
            row.backend_lease_until = now_utc() + timedelta(seconds=LEASE_SECONDS)
            db.add(AuditLog(
                actor=request.owner_login or "system",
                role="instructor" if request.owner_login else "system",
                action="lesson.start",
                object_id=str(row.id),
                detail=f"{request.scenario_id}, mode {request.mode}",
            ))
            service = None
            if row.trainee_id is not None:
                service = await db.scalar(
                    select(User.service)
                    .where(User.trainee_id == row.trainee_id, User.blocked.is_(False))
                    .limit(1)
                )
            epoch = row.backend_fencing_epoch
            state = build(LessonIdentity(
                attempt=row.attempt, trainee_id=row.trainee_id,
                service=service, fencing_epoch=epoch,
            ))
            if state.started_at is not None:
                row.started_at = state.started_at
            row.live_state = dump_state(state)
            row.checkpoint_at = now_utc()
            await db.commit()
        self._epochs[request.session_id] = epoch
        return state

    async def commit(self, state: SessionState, records: Sequence[Record] = ()) -> None:
        session_id = state.session_id
        if state.backend_fencing_epoch > 0:
            self._epochs.setdefault(session_id, state.backend_fencing_epoch)
        try:
            async with self._sessionmaker() as db:
                await self._fence(db, session_id, state.backend_fencing_epoch or None)
                for record in records:
                    await self._apply(db, session_id, record)
                values = (
                    {"live_state": None, "checkpoint_at": None}
                    if state.ended
                    else {"live_state": dump_state(state), "checkpoint_at": now_utc()}
                )
                await db.execute(
                    update(Session).where(Session.id == session_id).values(**values)
                )
                await db.commit()
        except SessionLeaseLost:
            raise
        except Exception as exc:
            # SQL-трасса несёт реплики и комментарии курсанта — в лог только тип.
            log.error("хранилище: commit занятия %s не удался (%s)",
                      session_id, type(exc).__name__)
            raise

    async def commit_archived(self, session_id: UUID, records: Sequence[Record]) -> None:
        """Занятия нет в памяти узла, владеть нечем: без lease и снимка."""
        try:
            async with self._sessionmaker() as db:
                for record in records:
                    await self._apply(db, session_id, record)
                await db.commit()
        except Exception as exc:
            log.error("хранилище: запись архивного занятия %s не удалась (%s)",
                      session_id, type(exc).__name__)
            raise

    async def _apply(self, db: AsyncSession, session_id: UUID, record: Record) -> None:
        match record:
            case UtteranceAppended(entry=entry):
                db.add(Utterance(
                    session_id=session_id, ref=entry.ref, speaker=entry.speaker.value,
                    text=entry.text, at=entry.at,
                    mood=entry.mood.value if entry.mood else None,
                ))
            case HintRecorded():
                db.add(HintUse(
                    session_id=session_id, checklist_id=record.checklist_id,
                    question=record.question, at=record.at,
                ))
            case NoteAdded():
                db.add(InstructorNote(
                    session_id=session_id, transcript_ref=record.transcript_ref,
                    text=record.text, author=record.author,
                ))
            case SelfAssessed():
                actor, role = await self._trainee_actor(db, session_id)
                db.add(SelfAssessment(
                    session_id=session_id, missed=record.missed,
                    comment=record.comment, submitted_at=record.at,
                ))
                db.add(AuditLog(
                    actor=actor, role=role, action="self_assessment.submit",
                    object_id=str(session_id),
                    detail=f"missed_count={len(record.missed)}; comment_chars={len(record.comment)}",
                ))
            case LessonPaused(at=at, author=author, role=role):
                db.add(AuditLog(
                    actor=author, role=role, action="session.pause",
                    object_id=str(session_id), detail=f"at={at.isoformat()}",
                ))
            case LessonResumed(at=at, author=author, role=role, paused_ms=paused_ms):
                db.add(AuditLog(
                    actor=author, role=role, action="session.resume",
                    object_id=str(session_id), detail=f"at={at.isoformat()}; paused_ms={paused_ms}",
                ))
            case LessonStarted(at=at):
                await db.execute(
                    update(Session).where(Session.id == session_id).values(started_at=at)
                )
            case LessonEnded(at=at, reason=reason):
                await db.execute(
                    update(Session).where(Session.id == session_id)
                    .values(ended_at=at, end_reason=reason)
                )
            case ScoreCalculated():
                db.add(Score(
                    session_id=session_id, score_auto=record.score_auto,
                    score_final=record.score_auto, report=record.report,
                ))
                db.add(AuditLog(
                    actor="system", role="system", action="score.calculate",
                    object_id=str(session_id), detail=f"score_auto={record.score_auto}",
                ))
            case ScoreArchived():
                await db.flush()
                await db.execute(
                    update(Score).where(Score.session_id == session_id)
                    .values(report=record.report)
                )
            case ScoreOverridden():
                await db.flush()
                score = await db.scalar(
                    select(Score).where(Score.session_id == session_id).with_for_update()
                )
                if score is None:
                    raise LookupError(f"нет оценки занятия {session_id}")
                score.score_final = record.score_final
                score.overridden_by = record.author
                score.override_comment = record.comment
                score.report = apply_score_override(dict(score.report or {}), record)
                db.add(AuditLog(
                    actor=record.author, role=record.role, action="score.override",
                    object_id=str(session_id),
                    # Обоснование остаётся в разборе; аудиту нужны изменение и автор,
                    # а не вторая бессрочная копия свободного текста.
                    detail=(f"{score.score_auto} → {record.score_final}; "
                            f"comment_chars={len(record.comment)}"),
                ))
        await db.flush()

    @staticmethod
    async def _trainee_actor(db: AsyncSession, session_id: UUID) -> tuple[str, str]:
        session = await db.get(Session, session_id)
        if session is None or session.trainee_id is None:
            return "system", "system"
        login = await db.scalar(select(User.login).where(User.trainee_id == session.trainee_id))
        return (login, "trainee") if login else (f"trainee:{session.trainee_id}", "trainee")

    async def renew(self, session_id: UUID) -> None:
        """Refresh an owned session lease; concurrent takeover is row-serialized."""
        async with self._sessionmaker() as db:
            await self._fence(db, session_id)
            await db.commit()

    async def claim_expired(self, session_id: UUID | None = None) -> list[SessionState]:
        """Atomically fence and restore expired owners on this backend node."""
        if self._node_id is None:
            return []
        now = now_utc()
        conditions = [
            Session.ended_at.is_(None),
            Session.live_state.is_not(None),
            Session.checkpoint_at.is_not(None),
            (Session.backend_node_id.is_(None) | (Session.backend_node_id != self._node_id)),
            (Session.backend_lease_until.is_(None) | (Session.backend_lease_until <= now)),
        ]
        if session_id is not None:
            conditions.append(Session.id == session_id)
        async with self._sessionmaker() as db:
            rows = (await db.scalars(
                select(Session).where(*conditions).with_for_update(skip_locked=True).limit(100)
            )).all()
            for row in rows:
                row.backend_node_id = self._node_id
                row.backend_fencing_epoch = max(1, row.backend_fencing_epoch + 1)
                row.backend_lease_until = now + timedelta(seconds=LEASE_SECONDS)
            if rows:
                await db.commit()
        if not rows:
            return []
        return await self.restore_active(bump_owned_epoch=False)

    async def restore_active(self, *, bump_owned_epoch: bool = True) -> list[SessionState]:
        """Восстановить только незавершённые сессии с валидным снимком."""
        restored: list[SessionState] = []
        async with self._sessionmaker() as db:
            active_with_snapshot = (
                Session.ended_at.is_(None),
                Session.live_state.is_not(None),
                Session.checkpoint_at.is_not(None),
            )
            if self._node_id is not None:
                # Adopt legacy unassigned snapshots exactly once. Concurrent
                # nodes lock disjoint rows; subsequent restores are owner-only.
                unassigned = (await db.scalars(
                    select(Session)
                    .where(*active_with_snapshot, Session.backend_node_id.is_(None))
                    .with_for_update(skip_locked=True)
                )).all()
                for row in unassigned:
                    row.backend_node_id = self._node_id
                    row.backend_fencing_epoch = max(1, row.backend_fencing_epoch + 1)
                    row.backend_lease_until = now_utc() + timedelta(seconds=LEASE_SECONDS)
                if unassigned:
                    await db.commit()
                # A restarted process with the same stable node ID is a new
                # owner generation. Bump before exposing any restored state.
                owned = (await db.scalars(
                    select(Session)
                    .where(*active_with_snapshot, Session.backend_node_id == self._node_id)
                    .with_for_update(skip_locked=True)
                )).all()
                if bump_owned_epoch:
                    for row in owned:
                        row.backend_fencing_epoch = max(1, row.backend_fencing_epoch + 1)
                        row.backend_lease_until = now_utc() + timedelta(seconds=LEASE_SECONDS)
                    if owned:
                        await db.commit()
                rows = (await db.scalars(
                    select(Session).where(
                        *active_with_snapshot,
                        Session.backend_node_id == self._node_id,
                    )
                )).all()
            else:
                rows = (await db.scalars(
                    select(Session).where(*active_with_snapshot)
                )).all()
            for row in rows:
                try:
                    state = load_state(row.live_state, row.checkpoint_at)
                    if state.session_id != row.id:
                        raise ValueError("ID снимка не совпадает с записью занятия")
                    state.owner_login = row.owner_login
                    state.backend_fencing_epoch = row.backend_fencing_epoch
                    self._epochs[row.id] = row.backend_fencing_epoch
                    # Стенограмма — по строкам реплик: снимок, записанный до
                    # перехода на единый commit, мог отстать от них.
                    utterances = (await db.scalars(
                        select(Utterance)
                        .where(Utterance.session_id == row.id)
                        .order_by(Utterance.at, Utterance.ref)
                    )).all()
                    if utterances:
                        state.transcript = [
                            TranscriptEntry(
                                ref=item.ref,
                                speaker=Speaker(item.speaker),
                                text=item.text,
                                at=item.at,
                                mood=Mood(item.mood) if item.mood else None,
                            )
                            for item in utterances
                        ]
                    restored.append(state)
                except Exception as exc:  # noqa: BLE001 — один снимок не блокирует весь стенд
                    log.error("журнал: снимок занятия %s повреждён (%s)",
                              row.id, type(exc).__name__)
        return restored
