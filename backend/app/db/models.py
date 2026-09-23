"""Таблицы. Профиль курсанта, дельта попыток и аналитика группы строятся
по этому журналу, а не по памяти процесса (docs/arch/BACKEND.md).

Группы и связь `trainee → group` заложены здесь с первой миграции, даже пустыми:
размечать накопленные сессии задним числом — лишняя работа и лишняя миграция.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _uuid_pk() -> Mapped[UUID]:
    return mapped_column(primary_key=True, default=uuid4)


class Group(Base):
    """Учебная группа. Единица аналитики: «70% группы не уточняют этаж»."""

    __tablename__ = "groups"

    id: Mapped[UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(120), unique=True)
    # NULL — legacy/admin-managed group; instructor-created groups are private.
    owner_login: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    trainees: Mapped[list["Trainee"]] = relationship(back_populates="group")


class Trainee(Base):
    __tablename__ = "trainees"

    id: Mapped[UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(120))
    group_id: Mapped[UUID | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    group: Mapped["Group | None"] = relationship(back_populates="trainees")
    sessions: Mapped[list["Session"]] = relationship(back_populates="trainee")


class LearningMaterial(Base):
    """Локальный учебный материал или загруженный преподавателем ресурс."""

    __tablename__ = "learning_materials"

    id: Mapped[UUID] = _uuid_pk()
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(4), default="L1")
    kind: Mapped[str] = mapped_column(String(16), default="text")
    body: Mapped[str] = mapped_column(Text, default="")
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("scenarios.id", ondelete="SET NULL"), nullable=True
    )
    file_name: Mapped[str | None] = mapped_column(String(240))
    media_type: Mapped[str | None] = mapped_column(String(120))
    file_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MaterialAssignment(Base):
    """Назначение материала курсанту и факт изучения."""

    __tablename__ = "material_assignments"

    id: Mapped[UUID] = _uuid_pk()
    material_id: Mapped[UUID] = mapped_column(
        ForeignKey("learning_materials.id", ondelete="CASCADE")
    )
    trainee_id: Mapped[UUID] = mapped_column(ForeignKey("trainees.id", ondelete="CASCADE"))
    assigned_by: Mapped[str] = mapped_column(String(80))
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("material_id", "trainee_id", name="uq_material_assignment"),
        Index("ix_material_assignments_trainee", "trainee_id", "assigned_at"),
    )


class Scenario(Base):
    """Сценарий целиком лежит в `body`: библиотека — контент, а не схема.
    Отдельными колонками вынесено только то, по чему идёт выборка в списке."""

    __tablename__ = "scenarios"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    incident_type: Mapped[str] = mapped_column(String(20))
    level: Mapped[str] = mapped_column(String(4))
    topics: Mapped[list] = mapped_column(JSONB, default=list)
    modes: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16), default="published")
    # У черновика и утверждённого авторского сценария ровно один владелец;
    # NULL означает базовую/унаследованную системную библиотеку.
    owner_login: Mapped[str | None] = mapped_column(String(80), nullable=True)
    body: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Session(Base):
    """Одна попытка одного курсанта по одному сценарию.

    Номер попытки хранится здесь, а не отдельной таблицей: дельта попыток
    считается запросом по (trainee_id, scenario_id, attempt).
    """

    __tablename__ = "sessions"

    id: Mapped[UUID] = _uuid_pk()
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenarios.id", ondelete="RESTRICT"))
    owner_login: Mapped[str | None] = mapped_column(String(80), nullable=True)
    trainee_id: Mapped[UUID | None] = mapped_column(ForeignKey("trainees.id", ondelete="SET NULL"))
    group_id: Mapped[UUID | None] = mapped_column(ForeignKey("groups.id", ondelete="SET NULL"))
    mode: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(16))
    kio: Mapped[dict] = mapped_column(JSONB, default=dict)
    # Последний подтверждённый снимок незавершённого занятия. Он очищается при
    # завершении и позволяет новому backend-процессу продолжить карточку/ДДС.
    live_state: Mapped[dict | None] = mapped_column(JSONB)
    checkpoint_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    trainee: Mapped["Trainee | None"] = relationship(back_populates="sessions")
    utterances: Mapped[list["Utterance"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_sessions_trainee_scenario", "trainee_id", "scenario_id"),
        Index("ix_sessions_group_created", "group_id", "created_at"),
    )


class Utterance(Base):
    """Реплика транскрипта. `ref` — якорь для пометок и отметок разбора."""

    __tablename__ = "utterances"

    id: Mapped[UUID] = _uuid_pk()
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    ref: Mapped[str] = mapped_column(String(40))
    speaker: Mapped[str] = mapped_column(String(10))
    text: Mapped[str] = mapped_column(Text)
    mood: Mapped[str | None] = mapped_column(String(16))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    session: Mapped["Session"] = relationship(back_populates="utterances")

    __table_args__ = (
        UniqueConstraint("session_id", "ref", name="uq_utterance_ref"),
        Index("ix_utterances_session", "session_id", "at"),
    )


class Finding(Base):
    """Отметка разбора. Без `fact` отметки не бывает: код без обоснования
    не появляется ни в одном интерфейсе."""

    __tablename__ = "findings"

    id: Mapped[UUID] = _uuid_pk()
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(4))
    source: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str] = mapped_column(Text)
    fact: Mapped[str] = mapped_column(Text)
    norm: Mapped[str | None] = mapped_column(Text)
    ref: Mapped[str | None] = mapped_column(String(80))
    competency: Mapped[str | None] = mapped_column(String(24))
    transcript_ref: Mapped[str | None] = mapped_column(String(40))
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_findings_session_code", "session_id", "code"),)


class HintUse(Base):
    """Использованная подсказка: какой пункт и на какой минуте.
    Штрафа нет — это информация преподавателю и материал разбора."""

    __tablename__ = "hint_uses"

    id: Mapped[UUID] = _uuid_pk()
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    checklist_id: Mapped[str] = mapped_column(String(60))
    question: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SelfAssessment(Base):
    """Самооценка до показа автооценки. Одна на сессию."""

    __tablename__ = "self_assessments"

    id: Mapped[UUID] = _uuid_pk()
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), unique=True
    )
    missed: Mapped[list] = mapped_column(JSONB, default=list)
    comment: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class InstructorNote(Base):
    """Пометка преподавателя к реплике. Видна курсанту в его истории."""

    __tablename__ = "instructor_notes"

    id: Mapped[UUID] = _uuid_pk()
    session_id: Mapped[UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    transcript_ref: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(120))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Score(Base):
    """Оценка сессии. Автооценка сохраняется рядом с коррекцией:
    видно, что скорректировано и кем."""

    __tablename__ = "scores"

    id: Mapped[UUID] = _uuid_pk()
    session_id: Mapped[UUID] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), unique=True
    )
    score_auto: Mapped[float] = mapped_column()
    score_final: Mapped[float] = mapped_column()
    overridden_by: Mapped[str | None] = mapped_column(String(120))
    override_comment: Mapped[str | None] = mapped_column(Text)
    report: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """Учётная запись. Три роли из ТЗ: администратор, преподаватель, обучающийся.

    `service` заполняется только у обучающегося и означает ДДС, в которой он
    работает: ТЗ требует, чтобы «в ленту попадали только профильные события»,
    а профильность определяется списком оповещения по ЕКП, больше ничем
    (docs/spec/DATASET.md). У преподавателя и администратора поле пустое.
    """

    __tablename__ = "users"

    id: Mapped[UUID] = _uuid_pk()
    login: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(16))
    service: Mapped[str | None] = mapped_column(String(120))
    trainee_id: Mapped[UUID | None] = mapped_column(ForeignKey("trainees.id", ondelete="SET NULL"))
    blocked: Mapped[bool] = mapped_column(default=False)
    # Версия полномочий попадает в подписанную cookie. Смена роли, пароля или
    # блокировки увеличивает её и отзывает старые cookie даже после restart.
    auth_version: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    """Журнал действий. ТЗ требует хранения не менее шести месяцев, поэтому
    записи не удаляются вместе с сессией: `object_id` — строка, а не ссылка.

    Пишется то, что меняет чужой результат или состав системы: запуск занятия,
    оценка, коррекция оценки, правка сценария, вход и выход.
    """

    __tablename__ = "audit_log"

    id: Mapped[UUID] = _uuid_pk()
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(80))
    role: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(40))
    object_id: Mapped[str | None] = mapped_column(String(80))
    detail: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (Index("ix_audit_at", "at"),)


class LlmCache(Base):
    """Кэш ответов LLM по хешу контекста. Работает и онлайн — экономия
    на повторах, — и как накопитель материала для офлайн-дерева."""

    __tablename__ = "llm_cache"

    context_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(80))
    prompt: Mapped[str] = mapped_column(Text)
    response: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
