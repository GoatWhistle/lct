"""Запись хода занятия в БД.

Профиль курсанта, дельта попыток и аналитика группы строятся по журналу,
а не по памяти процесса: всё, что здесь не записано, для оценки не существует.
"""

import logging
from datetime import datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import repo
from app.db.models import Score, SelfAssessment, Session

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
        self, session_id: UUID, scenario_id: str, mode: str, trainee_name: str | None
    ) -> int:
        """Завести сессию в журнале и вернуть номер попытки.

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
                )
                return row.attempt
        except Exception:  # noqa: BLE001 — журнал не должен ронять живую сессию
            log.exception("журнал: сессию завести не удалось")
            return 1

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
                .values(ended_at=at, end_reason=reason)
            )
            await db.commit()

        await self._write(lambda db: action(db))
