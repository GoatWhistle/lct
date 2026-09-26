"""Доступ к журналу. Всё, что не записано сюда, для оценки не существует."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Group, HintUse, InstructorNote, Session, Trainee, Utterance

BeforeSessionCommit = Callable[[AsyncSession, Session], None]


class SessionNodeConflict(PermissionError):
    """The session is routed to a backend other than its persisted owner."""


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
    session_id: UUID | None = None,
    owner_login: str | None = None,
    backend_node_id: str | None = None,
    before_commit: BeforeSessionCommit | None = None,
    commit: bool = True,
) -> Session:
    session = Session(
        scenario_id=scenario_id,
        mode=mode,
        owner_login=owner_login,
        backend_node_id=backend_node_id,
        trainee_id=trainee_id,
        group_id=group_id,
        attempt=await next_attempt(db, trainee_id, scenario_id),
    )
    if session_id is not None:
        session.id = session_id
    db.add(session)
    if before_commit is not None:
        await db.flush()
        before_commit(db, session)
    if commit:
        await db.commit()
    else:
        await db.flush()
    return session


async def ensure_session(
    db: AsyncSession,
    *,
    session_id: UUID,
    scenario_id: str,
    mode: str,
    trainee_name: str | None = None,
    trainee_id: UUID | None = None,
    group_name: str | None = None,
    owner_login: str | None = None,
    backend_node_id: str | None = None,
    before_commit: BeforeSessionCommit | None = None,
    commit: bool = True,
) -> Session:
    """Занятие, запущенное с пульта, должно иметь строку в журнале.

    Иначе реплики, подсказки и пометки не к чему привязать: они уходят
    в нарушение внешнего ключа, а профиль курсанта остаётся пустым.
    """
    existing = await db.scalar(
        select(Session)
        .where(Session.id == session_id)
        .with_for_update()
    )
    if existing is not None:
        if existing.owner_login != owner_login:
            raise PermissionError("занятие принадлежит другому преподавателю")
        if (
            existing.backend_node_id is not None
            and backend_node_id is not None
            and existing.backend_node_id != backend_node_id
        ):
            raise SessionNodeConflict("занятие закреплено за другим backend-узлом")
        changed = False
        if existing.backend_node_id is None and backend_node_id is not None:
            existing.backend_node_id = backend_node_id
            changed = True
        if before_commit is not None:
            before_commit(db, existing)
        if commit and (before_commit is not None or changed):
            await db.commit()
        return existing

    group = await ensure_group(db, group_name, owner_login=owner_login) if group_name else None
    trainee = await db.get(Trainee, trainee_id) if trainee_id else None
    if trainee_id and trainee is None:
        raise ValueError(f"курсант {trainee_id} не найден")
    if trainee is not None:
        await _assert_trainee_scope(db, trainee, owner_login)
    if trainee is None and trainee_name:
        trainee = await ensure_trainee(
            db, trainee_name, group, owner_login=owner_login
        )
    return await create_session(
        db,
        scenario_id=scenario_id,
        mode=mode,
        trainee_id=trainee.id if trainee else None,
        group_id=group.id if group else trainee.group_id if trainee else None,
        session_id=session_id,
        owner_login=owner_login,
        backend_node_id=backend_node_id,
        before_commit=before_commit,
        commit=commit,
    )


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
    owner_login: str | None = None,
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
    if owner_login is not None:
        query = query.where(Session.owner_login == owner_login)
    if since is not None:
        query = query.where(Session.created_at >= since)
    return list(await db.scalars(query))


async def ensure_group(
    db: AsyncSession, name: str, *, owner_login: str | None = None, commit: bool = True
) -> Group:
    group = await db.scalar(select(Group).where(Group.name == name))
    if group is None:
        group = Group(name=name, owner_login=owner_login)
        db.add(group)
        if commit:
            await db.commit()
        else:
            await db.flush()
    elif group.owner_login != owner_login:
        raise PermissionError("группа принадлежит другому преподавателю или администратору")
    return group


async def ensure_trainee(
    db: AsyncSession,
    name: str,
    group: Group | None = None,
    *,
    owner_login: str | None = None,
    commit: bool = True,
) -> Trainee:
    query = select(Trainee).where(Trainee.name == name)
    trainee = await db.scalar(query)
    if trainee is None:
        if group is not None and owner_login is not None and group.owner_login != owner_login:
            raise PermissionError("курсант относится к другой группе")
        trainee = Trainee(name=name, group_id=group.id if group else None)
        db.add(trainee)
        if commit:
            await db.commit()
        else:
            await db.flush()
    else:
        await _assert_trainee_scope(db, trainee, owner_login)
    return trainee


async def _assert_trainee_scope(
    db: AsyncSession, trainee: Trainee, owner_login: str | None
) -> None:
    """Prevent lesson creation from attaching a learner owned by another teacher."""
    if owner_login is None or trainee.group_id is None:
        return
    group = await db.get(Group, trainee.group_id)
    if group is None or group.owner_login != owner_login:
        raise PermissionError("курсант относится к другой учебной группе")
