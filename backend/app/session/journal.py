"""Запись хода занятия в БД.

Профиль курсанта, дельта попыток и аналитика группы строятся по журналу,
а не по памяти процесса: всё, что здесь не записано, для оценки не существует.
"""

import logging
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import repo
from app.db.models import AuditLog, Score, SelfAssessment, Session, User, Utterance
from app.domain.events import Mood, Speaker, TranscriptEntry
from app.session.checkpoint import dump_state, load_state
from app.session.state import SessionState, now_utc

log = logging.getLogger(__name__)
LEASE_SECONDS = 15


class SessionLeaseLost(RuntimeError):
    """This process no longer owns the durable session generation."""


class DbJournal:
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

    async def _write(
        self, action, *args, _fence_session_id: UUID | None = None,
        _fence_epoch: int | None = None, _raise_errors: bool = False, **kwargs
    ) -> None:
        """Ошибка записи не роняет занятие, но и не проглатывается молча:
        занятие идёт дальше, в логе остаётся след."""
        try:
            async with self._sessionmaker() as db:
                if _fence_session_id is not None:
                    await self._fence(db, _fence_session_id, _fence_epoch)
                await action(db, *args, **kwargs)
        except SessionLeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001 — журнал не должен ронять живую сессию
            session_id = _fence_session_id or kwargs.get("session_id")
            log.error("журнал: запись не удалась для сессии %s (%s)",
                      session_id, type(exc).__name__)
            if _raise_errors:
                raise

    async def start_lesson(
        self, session_id: UUID, scenario_id: str, mode: str, trainee_name: str | None,
        trainee_id: UUID | None = None, owner_login: str | None = None,
        backend_node_id: str | None = None,
    ) -> tuple[int, UUID | None, str | None, int] | None:
        """Завести сессию в журнале и вернуть номер попытки и ID курсанта.

        Строка сессии и событие аудита фиксируются вместе. При сбое транзакции
        занятие не запускается без долговечной истории.
        """
        try:
            async with self._sessionmaker() as db:
                node_id = backend_node_id or self._node_id

                def audit_start(transaction, row):
                    if row.backend_fencing_epoch <= 0:
                        row.backend_fencing_epoch = 1
                    row.backend_lease_until = now_utc() + timedelta(seconds=LEASE_SECONDS)
                    transaction.add(AuditLog(
                        actor=owner_login or "system",
                        role="instructor" if owner_login else "system",
                        action="lesson.start",
                        object_id=str(row.id),
                        detail=f"{scenario_id}, mode {mode}",
                    ))

                row = await repo.ensure_session(
                    db,
                    session_id=session_id,
                    scenario_id=scenario_id,
                    mode=mode,
                    trainee_name=trainee_name,
                    trainee_id=trainee_id,
                    owner_login=owner_login,
                    backend_node_id=node_id,
                    before_commit=audit_start,
                )
                epoch = getattr(row, "backend_fencing_epoch", 0) or 1
                self._epochs[session_id] = epoch
                service = None
                if row.trainee_id is not None:
                    service = await db.scalar(
                        select(User.service)
                        .where(User.trainee_id == row.trainee_id, User.blocked.is_(False))
                        .limit(1)
                    )
                return row.attempt, row.trainee_id, service, epoch
        except PermissionError:
            raise
        except Exception as exc:  # noqa: BLE001 — журнал не должен ронять живую сессию
            log.error("журнал: не удалось завести сессию %s (%s)",
                      session_id, type(exc).__name__)
            return None

    async def checkpoint(self, state: SessionState) -> None:
        """Сохранить снимок после подтверждённого действия пользователя."""
        async def action(db):
            values = (
                {"live_state": None, "checkpoint_at": None}
                if state.ended
                else {"live_state": dump_state(state), "checkpoint_at": now_utc()}
            )
            await db.execute(update(Session).where(Session.id == state.session_id).values(**values))
            await db.commit()

        if state.backend_fencing_epoch > 0:
            self._epochs.setdefault(state.session_id, state.backend_fencing_epoch)
        await self._write(
            lambda db: action(db), _fence_session_id=state.session_id,
            _fence_epoch=state.backend_fencing_epoch or None,
            _raise_errors=True,
        )

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
                    # Реплики пишутся отдельно сразу после появления. Если
                    # процесс умер между репликой и общим снимком, отдельный
                    # журнал не даёт потерять последний фрагмент диалога.
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

    async def utterance(self, session_id: UUID, entry) -> None:
        await self._write(
            repo.append_utterance,
            session_id=session_id,
            ref=entry.ref,
            speaker=entry.speaker.value,
            text=entry.text,
            at=entry.at,
            mood=entry.mood.value if entry.mood else None,
            _fence_session_id=session_id,
        )

    async def hint(self, session_id: UUID, checklist_id: str, question: str, at: datetime) -> None:
        await self._write(
            repo.record_hint, _fence_session_id=session_id, session_id=session_id,
            checklist_id=checklist_id, question=question, at=at
        )

    async def note(self, session_id: UUID, ref: str, text: str, author: str) -> None:
        await self._write(
            repo.add_note, _fence_session_id=session_id, session_id=session_id,
            transcript_ref=ref, text=text, author=author
        )

    async def self_assessment(
        self, session_id: UUID, missed: list[str], comment: str, at: datetime
    ) -> bool:
        """Persist trainee reflection and its security audit together."""
        try:
            async with self._sessionmaker() as db:
                session = await db.get(Session, session_id)
                if session is None:
                    return False
                actor = "system"
                role = "system"
                if session.trainee_id is not None:
                    login = await db.scalar(
                        select(User.login).where(User.trainee_id == session.trainee_id)
                    )
                    if login:
                        actor, role = login, "trainee"
                    else:
                        actor, role = f"trainee:{session.trainee_id}", "trainee"
                db.add(SelfAssessment(
                    session_id=session_id, missed=missed, comment=comment, submitted_at=at
                ))
                db.add(AuditLog(
                    actor=actor,
                    role=role,
                    action="self_assessment.submit",
                    object_id=str(session_id),
                    detail=f"missed_count={len(missed)}; comment_chars={len(comment)}",
                ))
                await self._fence(db, session_id)
                await db.commit()
                return True
        except SessionLeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001 — do not accept an unaudited reflection
            log.error("самооценка и аудит сессии %s не сохранены (%s)",
                      session_id, type(exc).__name__)
            return False

    async def score(self, session_id: UUID, score_auto: float, report: dict) -> bool:
        """Persist the initial result and its audit event atomically."""
        try:
            async with self._sessionmaker() as db:
                await self._fence(db, session_id)
                db.add(Score(
                    session_id=session_id, score_auto=score_auto,
                    score_final=score_auto, report=report,
                ))
                db.add(AuditLog(
                    actor="system", role="system", action="score.calculate",
                    object_id=str(session_id), detail=f"score_auto={score_auto}",
                ))
                await db.commit()
                return True
        except SessionLeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001 — result is not complete until durable
            log.error("итоговая оценка и аудит сессии %s не сохранены (%s)",
                      session_id, type(exc).__name__)
            return False

    async def score_override(
        self, session_id: UUID, score_final: float, author: str, comment: str
    ) -> bool:
        """Persist a live correction and its security audit as one transaction."""
        try:
            async with self._sessionmaker() as db:
                await self._fence(db, session_id)
                score = await db.scalar(
                    select(Score)
                    .where(Score.session_id == session_id)
                    .with_for_update()
                )
                if score is None:
                    return False
                score.score_final = score_final
                score.overridden_by = author
                score.override_comment = comment
                report = dict(score.report or {})
                archived = report.get("full_report")
                if isinstance(archived, dict):
                    archived = dict(archived)
                    archived.update({
                        "score_auto": score.score_auto,
                        "score_final": score_final,
                        "overridden_by": author,
                        "override_comment": comment,
                    })
                    report["full_report"] = archived
                    score.report = report
                db.add(AuditLog(
                    actor=author,
                    role="instructor",
                    action="score.override",
                    object_id=str(session_id),
                    detail=(f"{score.score_auto} → {score_final}; "
                            f"comment_chars={len(comment)}"),
                ))
                await db.commit()
                return True
        except SessionLeaseLost:
            raise
        except Exception as exc:  # noqa: BLE001 — do not confirm a correction without its audit
            log.error("корректировка оценки и аудит сессии %s не сохранены (%s)",
                      session_id, type(exc).__name__)
            return False

    async def score_snapshot(self, session_id: UUID, report: dict) -> None:
        """Обновить полный архивный разбор после самооценки курсанта."""
        async def action(db):
            await db.execute(
                update(Score).where(Score.session_id == session_id).values(report=report)
            )
            await db.commit()

        await self._write(lambda db: action(db), _fence_session_id=session_id)

    async def session_started(self, session_id: UUID, at: datetime) -> None:
        async def action(db):
            await db.execute(update(Session).where(Session.id == session_id).values(started_at=at))
            await db.commit()

        await self._write(lambda db: action(db), _fence_session_id=session_id)

    async def session_ended(self, session_id: UUID, at: datetime, reason: str) -> None:
        async def action(db):
            await db.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(
                    ended_at=at,
                    end_reason=reason,
                    live_state=None,
                    checkpoint_at=None,
                )
            )
            await db.commit()

        await self._write(lambda db: action(db), _fence_session_id=session_id)
