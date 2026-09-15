"""Доступ к журналу. Всё, что не записано сюда, для оценки не существует."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, HintUse, InstructorNote, Session, Trainee, Utterance


async def next_attempt(db: AsyncSession, trainee_id: UUID | None, scenario_id: str) -> int:
    """Номер попытки по этому сценарию. Отдельной таблицы попыток нет:
    дельта считается запросом по (trainee_id, scenario_id, attempt)."""
    if trainee_id is None:
        return 1
    done = await db.scalar(
        select(func.count())
        .select_from(Session)
        .where(Session.trainee_id == trainee_id, Session.scenario_id == scenario_id)
    )
    return int(done or 0) + 1


async def create_session(
    db: AsyncSession,
    *,
    scenario_id: str,
    mode: str,
    trainee_id: UUID | None = None,
    group_id: UUID | None = None,
) -> Session:
    session = Session(
        scenario_id=scenario_id,
        mode=mode,
        trainee_id=trainee_id,
        group_id=group_id,
        attempt=await next_attempt(db, trainee_id, scenario_id),
    )
    db.add(session)
    await db.commit()
    return session


async def get_session(db: AsyncSession, session_id: UUID) -> Session | None:
    return await db.get(Session, session_id)


async def append_utterance(
    db: AsyncSession,
    *,
    session_id: UUID,
    ref: str,
    speaker: str,
    text: str,
    at: datetime,
    mood: str | None = None,
) -> Utterance:
    utterance = Utterance(
        session_id=session_id, ref=ref, speaker=speaker, text=text, at=at, mood=mood
    )
    db.add(utterance)
    await db.commit()
    return utterance


async def transcript(db: AsyncSession, session_id: UUID) -> list[Utterance]:
    rows = await db.scalars(
        select(Utterance).where(Utterance.session_id == session_id).order_by(Utterance.at)
    )
    return list(rows)


async def record_hint(
    db: AsyncSession, *, session_id: UUID, checklist_id: str, question: str, at: datetime
) -> HintUse:
    """Каждое использование подсказки попадает в журнал: счёт подсказок —
    материал разбора, а не вычитаемое из баллов."""
    hint = HintUse(session_id=session_id, checklist_id=checklist_id, question=question, at=at)
    db.add(hint)
    await db.commit()
    return hint


async def add_note(
    db: AsyncSession, *, session_id: UUID, transcript_ref: str, text: str, author: str
) -> InstructorNote:
    note = InstructorNote(
        session_id=session_id, transcript_ref=transcript_ref, text=text, author=author
    )
    db.add(note)
    await db.commit()
    return note


async def history(
    db: AsyncSession,
    *,
    trainee_id: UUID | None = None,
    group_id: UUID | None = None,
    mode: str | None = None,
    since: datetime | None = None,
    limit: int = 100,
) -> list[Session]:
    """История с фильтрами: преподавателю нужно видеть прогресс группы
    и самостоятельную работу курсантов."""
    query = select(Session).order_by(Session.created_at.desc()).limit(limit)
    if trainee_id is not None:
        query = query.where(Session.trainee_id == trainee_id)
    if group_id is not None:
        query = query.where(Session.group_id == group_id)
    if mode is not None:
        query = query.where(Session.mode == mode)
    if since is not None:
        query = query.where(Session.created_at >= since)
    return list(await db.scalars(query))


async def ensure_group(db: AsyncSession, name: str) -> Group:
    group = await db.scalar(select(Group).where(Group.name == name))
    if group is None:
        group = Group(name=name)
        db.add(group)
        await db.commit()
    return group


async def ensure_trainee(db: AsyncSession, name: str, group: Group | None = None) -> Trainee:
    query = select(Trainee).where(Trainee.name == name)
    trainee = await db.scalar(query)
    if trainee is None:
        trainee = Trainee(name=name, group_id=group.id if group else None)
        db.add(trainee)
        await db.commit()
    return trainee
