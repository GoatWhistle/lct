"""Запись хода занятия в БД.

Профиль курсанта, дельта попыток и аналитика группы строятся по журналу,
а не по памяти процесса: всё, что здесь не записано, для оценки не существует.
"""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import repo
from app.db.models import Score, SelfAssessment, Session, User, Utterance
from app.domain.events import Mood, Speaker, TranscriptEntry
from app.session.checkpoint import dump_state, load_state
from app.session.state import SessionState, now_utc

log = logging.getLogger(__name__)


class DbJournal:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def _write(self, action, *args, **kwargs) -> None:
        """Ошибка записи не роняет занятие, но и не проглатывается молча:
        занятие идёт дальше, в логе остаётся след."""
        try:
            async with self._sessionmaker() as db:
                await action(db, *args, **kwargs)
        except Exception:  # noqa: BLE001 — журнал не должен ронять живую сессию
            log.exception("журнал: запись не удалась")

    async def start_lesson(
        self, session_id: UUID, scenario_id: str, mode: str, trainee_name: str | None,
        trainee_id: UUID | None = None, owner_login: str | None = None,
    ) -> tuple[int, UUID | None, str | None]:
        """Завести сессию в журнале и вернуть номер попытки и ID курсанта.

        Если база недоступна, занятие всё равно идёт: номер попытки
        деградирует до первого, и это видно в логе.
        """
        try:
            async with self._sessionmaker() as db:
                row = await repo.ensure_session(
                    db,
                    session_id=session_id,
                    scenario_id=scenario_id,
                    mode=mode,
                    trainee_name=trainee_name,
                    trainee_id=trainee_id,
                    owner_login=owner_login,
                )
                service = None
                if row.trainee_id is not None:
                    service = await db.scalar(
                        select(User.service)
                        .where(User.trainee_id == row.trainee_id, User.blocked.is_(False))
                        .limit(1)
                    )
                return row.attempt, row.trainee_id, service
        except PermissionError:
            raise
        except Exception:  # noqa: BLE001 — журнал не должен ронять живую сессию
            log.exception("журнал: сессию завести не удалось")
            return 1, trainee_id, None

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

        await self._write(lambda db: action(db))

    async def restore_active(self) -> list[SessionState]:
        """Восстановить только незавершённые сессии с валидным снимком."""
        restored: list[SessionState] = []
        async with self._sessionmaker() as db:
            rows = (await db.scalars(
                select(Session).where(
                    Session.ended_at.is_(None),
                    Session.live_state.is_not(None),
                    Session.checkpoint_at.is_not(None),
                )
            )).all()
            for row in rows:
                try:
                    state = load_state(row.live_state, row.checkpoint_at)
                    if state.session_id != row.id:
                        raise ValueError("ID снимка не совпадает с записью занятия")
                    state.owner_login = row.owner_login
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
                except Exception:  # noqa: BLE001 — один снимок не блокирует весь стенд
                    log.exception("журнал: снимок занятия %s повреждён", row.id)
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
        )

    async def hint(self, session_id: UUID, checklist_id: str, question: str, at: datetime) -> None:
        await self._write(
            repo.record_hint, session_id=session_id, checklist_id=checklist_id, question=question, at=at
        )

    async def note(self, session_id: UUID, ref: str, text: str, author: str) -> None:
        await self._write(repo.add_note, session_id=session_id, transcript_ref=ref, text=text, author=author)

    async def self_assessment(
        self, session_id: UUID, missed: list[str], comment: str, at: datetime
    ) -> None:
        async def action(db):
            db.add(
                SelfAssessment(
                    session_id=session_id, missed=missed, comment=comment, submitted_at=at
                )
            )
            await db.commit()

        await self._write(lambda db: action(db))

    async def score(self, session_id: UUID, score_auto: float, report: dict) -> None:
        async def action(db):
            db.add(Score(session_id=session_id, score_auto=score_auto, score_final=score_auto, report=report))
            await db.commit()

        await self._write(lambda db: action(db))

    async def score_override(
        self, session_id: UUID, score_final: float, author: str, comment: str
    ) -> None:
        """Сохранить решение преподавателя рядом с неизменной автооценкой."""
        async def action(db):
            await db.execute(
                update(Score)
                .where(Score.session_id == session_id)
                .values(
                    score_final=score_final,
                    overridden_by=author,
                    override_comment=comment,
                )
            )
            await db.commit()

        await self._write(lambda db: action(db))

    async def score_snapshot(self, session_id: UUID, report: dict) -> None:
        """Обновить полный архивный разбор после самооценки курсанта."""
        async def action(db):
            await db.execute(
                update(Score).where(Score.session_id == session_id).values(report=report)
            )
            await db.commit()

        await self._write(lambda db: action(db))

    async def session_started(self, session_id: UUID, at: datetime) -> None:
        async def action(db):
            await db.execute(update(Session).where(Session.id == session_id).values(started_at=at))
            await db.commit()

        await self._write(lambda db: action(db))

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

        await self._write(lambda db: action(db))
