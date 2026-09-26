"""Библиотека сценариев по HTTP.

Курсантский каталог и карточка отдают только заголовок, сложность и доступные
режимы: классификатор, факты, личность звонящего и чек-лист не должны быть
доступны заранее через DevTools. Инструктор и администратор получают редакторскую
карточку. Подсказки в сессии выдаются по одному пункту через `hint.shown`,
эталонные вопросы — только в разборе (docs/product/MODES.md#подсказка-по-запросу).
"""

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import add_audit_entry, audit, require
from app.config import get_settings
from app.db.base import get_session
from app.db.models import Group, Trainee
from app.dialog.llm import LlmUnavailable
from app.domain import ekp
from app.domain.roles import Role
from app.scenarios import store
from app.scenarios.editor import validate
from app.scenarios.generation import (
    GenerationError,
    generate,
    generate_from_description,
)
from app.scenarios.loader import ScenarioError
from app.scoring.grammar import assess
from app.session.hub import hub

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


async def _hidden_scenario_ids(db: AsyncSession | None, who) -> set[str]:
    """Scenario drafts are private to their instructor and that instructor's class."""
    if who.role is Role.ADMIN:
        return set()
    owner_login = who.login
    if who.role is Role.TRAINEE:
        if db is None or who.trainee_id is None:
            owner_login = ""
        else:
            owner_login = await db.scalar(
                select(Group.owner_login)
                .join(Trainee, Trainee.group_id == Group.id)
                .where(Trainee.id == who.trainee_id)
            ) or ""
    return await store.scenario_ids_owned_by_other(db, owner_login)


async def scenario_session() -> AsyncIterator[AsyncSession | None]:
    """Только редактор в demo-lite использует временное хранилище без БД."""
    if get_settings().demo_no_db:
        yield None
    else:
        async for db in get_session():
            yield db


class TemplateDraftIn(BaseModel):
    source_id: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)


class GenerateDraftIn(BaseModel):
    source_id: str = Field(min_length=1)
    instruction: str = Field(min_length=10, max_length=1000)


class GenerateFullDraftIn(BaseModel):
    source_id: str = Field(min_length=1)
    description: str = Field(min_length=20, max_length=1500)


class ReviseDraftIn(BaseModel):
    comment: str = Field(min_length=10, max_length=1000)


def _draft_out(row) -> dict:
    if row.id.startswith("ai-full-"):
        generation = "ai_full"
    elif row.id.startswith("ai-"):
        generation = "ai_variant"
    else:
        generation = "template_copy"
    return {
        "id": row.id,
        "status": row.status,
        "generation": generation,
        "body": row.body,
    }


def _draft_grammar_hash(scenario) -> str:
    """Stable fingerprint of the caller dialogue fields covered by grammar QA."""
    payload = {
        "first_line": scenario.first_line,
        "facts": [
            {"id": fact.id, "value": fact.value, "refined": fact.refined}
            for fact in scenario.facts
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _audit_before_commit(actor: str, role: str, action: str, detail: str = ""):
    return lambda transaction, row: add_audit_entry(
        transaction, actor, role, action, str(row.id), detail
    )


@router.post("/drafts/from-template", status_code=201)
async def create_template_draft(
    body: TemplateDraftIn, request: Request, db: AsyncSession | None = Depends(scenario_session)
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    source = store.get(body.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="published_source_not_found")
    row = await store.create_draft(
        db,
        source=source,
        title=body.title,
        owner_login=who.login,
        before_commit=_audit_before_commit(
            who.login, who.role.value, "scenario.draft.create", f"template:{source.id}"
        ),
    )
    if db is None:
        await audit(who.login, who.role.value, "scenario.draft.create", row.id, f"template:{source.id}")
    return _draft_out(row)


@router.post("/drafts/generate", status_code=201)
async def create_ai_draft(
    body: GenerateDraftIn, request: Request, db: AsyncSession | None = Depends(scenario_session)
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    source = store.get(body.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="published_source_not_found")
    try:
        proposal = await generate(source, body.instruction.strip(), require_fact_change=False)
        row = await store.create_draft(
            db, source=source, proposal=proposal, owner_login=who.login,
            before_commit=_audit_before_commit(
                who.login, who.role.value, "scenario.draft.ai_generate", f"source:{source.id}",
            ),
        )
    except LlmUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"локальная модель недоступна: {exc}") from exc
    except GenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if db is None:
        await audit(who.login, who.role.value, "scenario.draft.ai_generate", row.id,
                    f"source:{source.id}")
    return _draft_out(row)


@router.post("/drafts/generate-from-description", status_code=201)
async def create_full_ai_draft(
    body: GenerateFullDraftIn, request: Request,
    db: AsyncSession | None = Depends(scenario_session),
) -> dict:
    """Новый сюжет и мягкий эталон внутри выбранного класса ЕКП."""
    who = require(request, Role.INSTRUCTOR)
    source = store.get(body.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="published_source_not_found")
    try:
        proposal = await generate_from_description(source, body.description.strip())
        row = await store.create_draft(
            db, source=source, full_proposal=proposal, owner_login=who.login,
            before_commit=_audit_before_commit(
                who.login, who.role.value, "scenario.draft.ai_generate_full",
                f"class_source:{source.id}",
            ),
        )
    except LlmUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"локальная модель недоступна: {exc}") from exc
    except GenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if db is None:
        await audit(who.login, who.role.value, "scenario.draft.ai_generate_full", row.id,
                    f"class_source:{source.id}")
    return _draft_out(row)


@router.get("/drafts/{scenario_id}")
async def read_draft(
    scenario_id: str, request: Request, db: AsyncSession | None = Depends(scenario_session)
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    row = await store.draft(db, scenario_id, owner_login=who.login)
    if row is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    return _draft_out(row)


@router.patch("/drafts/{scenario_id}")
async def patch_draft(
    scenario_id: str,
    body: dict[str, Any],
    request: Request,
    db: AsyncSession | None = Depends(scenario_session),
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    row = await store.draft(db, scenario_id, owner_login=who.login)
    if row is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    try:
        row = await store.update_draft(
            db, row, body,
            before_commit=_audit_before_commit(
                who.login, who.role.value, "scenario.draft.update"
            ),
        )
    except ScenarioError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if db is None:
        await audit(who.login, who.role.value, "scenario.draft.update", row.id)
    return _draft_out(row)


@router.post("/drafts/{scenario_id}/revise")
async def revise_ai_draft(
    scenario_id: str,
    body: ReviseDraftIn,
    request: Request,
    db: AsyncSession | None = Depends(scenario_session),
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    row = await store.draft(db, scenario_id, owner_login=who.login)
    if row is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    try:
        source = validate(row.body)
        proposal = await generate(source, body.comment.strip(), require_fact_change=False)
        row = await store.revise_draft(
            db, row, proposal,
            before_commit=_audit_before_commit(
                who.login, who.role.value, "scenario.draft.ai_revise",
                f"instruction_chars={len(body.comment.strip())}",
            ),
        )
    except LlmUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"локальная модель недоступна: {exc}") from exc
    except (GenerationError, ScenarioError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Editorial instructions can contain names, addresses, or other sensitive
    # details. Keep only non-content metadata in the durable admin audit log.
    if db is None:
        await audit(who.login, who.role.value, "scenario.draft.ai_revise", row.id,
                    f"instruction_chars={len(body.comment.strip())}")
    return _draft_out(row)


@router.post("/drafts/{scenario_id}/validate")
async def validate_draft(
    scenario_id: str, request: Request, db: AsyncSession | None = Depends(scenario_session)
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    row = await store.draft(db, scenario_id, owner_login=who.login)
    if row is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    try:
        scenario = validate(row.body)
    except ScenarioError as exc:
        return {"valid": False, "errors": [str(exc)]}
    return {
        "valid": True,
        "errors": [],
        "ground_truth": scenario.ground_truth.model_dump(mode="json"),
    }


@router.post("/drafts/{scenario_id}/grammar-check")
async def check_draft_grammar(
    scenario_id: str, request: Request, db: AsyncSession | None = Depends(scenario_session)
) -> dict:
    """Явная языковая проверка после ручного редактирования сценария.

    Это только диагностический результат: проверяются реплика звонящего и
    текстовые значения фактов, но содержимое не исправляется и не публикуется.
    """
    who = require(request, Role.INSTRUCTOR)
    row = await store.draft(db, scenario_id, owner_login=who.login)
    if row is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    try:
        scenario = validate(row.body)
    except ScenarioError as exc:
        raise HTTPException(status_code=422, detail=f"сначала исправьте структуру: {exc}") from exc

    fields = [("first_line", scenario.first_line)]
    for fact in scenario.facts:
        fields.append((f"facts.{fact.id}.value", fact.value))
        if fact.refined:
            fields.append((f"facts.{fact.id}.refined", fact.refined))
    checks = []
    for field, value in fields:
        result = await assess(value)
        checks.append({
            "field": field,
            "passed": result.passed,
            "errors": list(result.errors),
            "source": result.source,
        })
    passed = all(item["passed"] for item in checks)
    row.grammar_check_hash = _draft_grammar_hash(scenario) if passed else None
    if db is not None:
        await db.commit()
    return {"valid": passed, "checks": checks}


@router.post("/drafts/{scenario_id}/approve")
async def approve_draft(
    scenario_id: str, request: Request, db: AsyncSession | None = Depends(scenario_session)
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    row = await store.draft(db, scenario_id, owner_login=who.login)
    if row is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    try:
        current = validate(row.body)
        if (row.manual_edit_pending
                and row.grammar_check_hash != _draft_grammar_hash(current)):
            raise HTTPException(
                status_code=409,
                detail="после ручных правок требуется успешная проверка грамматики",
            )
        scenario = await store.approve_draft(
            db, row,
            before_commit=_audit_before_commit(
                who.login, who.role.value, "scenario.approve"
            ),
        )
    except ScenarioError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if db is None:
        await audit(who.login, who.role.value, "scenario.approve", scenario.id)
    return {"id": scenario.id, "status": "published", "title": scenario.title}


@router.get("")
async def listing(
    request: Request, db: AsyncSession | None = Depends(scenario_session)
) -> list[dict]:
    who = require(request, Role.INSTRUCTOR, Role.ADMIN, Role.TRAINEE)
    if db is not None:
        # Published student scenarios may have been approved on a peer backend.
        # Refresh this process-local catalog from the shared authoritative DB.
        await store.restore_published(db)
    owned_ids = (
        await store.owned_scenario_ids(db, who.login)
        if who is not None and who.role is Role.INSTRUCTOR
        else set()
    )
    hidden_ids = await _hidden_scenario_ids(db, who)
    result = []
    for scenario in store.all_scenarios():
        if scenario.id in hidden_ids:
            continue
        if who.role is Role.TRAINEE:
            # A trainee may select a scenario for self-practice, but the catalog
            # must not reveal dispatch codes, answer hints, or instructor-only metadata.
            if "self" not in scenario.modes:
                continue
            result.append({
                "id": scenario.id,
                "title": scenario.title,
                "level": scenario.level.value,
                "modes": scenario.modes,
            })
            continue
        result.append({
            "id": scenario.id,
            "title": scenario.title,
            "outcome": scenario.outcome.value,
            "type": scenario.type.value,
            "level": scenario.level.value,
            "topics": scenario.topics,
            "modes": scenario.modes,
            # Преподаватель должен видеть не только название карточки, но и
            # зафиксированный путь классификатора. ИИ меняет сюжет внутри
            # этого пути, а не незаметно подменяет код происшествия.
            "signs": scenario.signs,
            "incident_code": scenario.ground_truth.incident_code,
            "dds": scenario.ground_truth.dds.value if scenario.ground_truth.dds else None,
            "ticket": scenario.ticket,
            "position": scenario.position,
            "ekp_group": (ekp.incident(scenario.ground_truth.incident_code).group
                          if scenario.ground_truth.incident_code
                          and ekp.incident(scenario.ground_truth.incident_code) else None),
            "can_manage": scenario.id in owned_ids,
            "source": "trainee" if "student-created" in scenario.topics else "system",
        })
    return result


@router.delete("/{scenario_id}")
async def archive_scenario(
    scenario_id: str, request: Request,
    db: AsyncSession | None = Depends(scenario_session),
) -> dict:
    """Мягкое удаление: история занятий остаётся целой, сценарий можно вернуть."""
    who = require(request, Role.INSTRUCTOR)
    if hub.has_active_scenario(scenario_id):
        raise HTTPException(status_code=409, detail="scenario_is_used_by_active_session")
    scenario = await store.archive(
        db, scenario_id, owner_login=who.login,
        before_commit=_audit_before_commit(
            who.login, who.role.value, "scenario.archive"
        ),
    )
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario_not_found")
    if db is None:
        await audit(who.login, who.role.value, "scenario.archive", scenario_id)
    return {"id": scenario_id, "status": "archived", "title": scenario.title}


@router.post("/{scenario_id}/restore")
async def restore_scenario(
    scenario_id: str, request: Request,
    db: AsyncSession | None = Depends(scenario_session),
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    scenario = await store.restore_archived(
        db, scenario_id, owner_login=who.login,
        before_commit=_audit_before_commit(
            who.login, who.role.value, "scenario.restore"
        ),
    )
    if scenario is None:
        raise HTTPException(status_code=404, detail="archived_scenario_not_found")
    if db is None:
        await audit(who.login, who.role.value, "scenario.restore", scenario_id)
    return {"id": scenario_id, "status": "published", "title": scenario.title}


@router.get("/{scenario_id}")
async def read(
    scenario_id: str,
    request: Request,
    db: AsyncSession | None = Depends(scenario_session),
) -> dict:
    # Training content is local but not public: anonymous clients must not be
    # able to enumerate cards or inspect even the trainee-safe scenario body.
    who = require(request, Role.INSTRUCTOR, Role.ADMIN, Role.TRAINEE)
    if scenario_id in await _hidden_scenario_ids(db, who):
        raise HTTPException(status_code=404, detail="scenario_not_found")
    if db is not None:
        await store.restore_published(db)
    scenario = store.get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario_not_found")
    if who.role is Role.TRAINEE:
        if "self" not in scenario.modes:
            raise HTTPException(status_code=404, detail="scenario_not_found")
        return {
            "id": scenario.id,
            "title": scenario.title,
            "level": scenario.level.value,
            "modes": scenario.modes,
        }
    return scenario.model_dump(mode="json")
