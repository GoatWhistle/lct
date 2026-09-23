"""Библиотека сценариев по HTTP.

`GET /api/scenarios/{id}` **не отдаёт** `facts` и `ground_truth`: иначе курсант
откроет DevTools и прочитает адрес до того, как его спросит.

`checklist` скрыт по той же причине и даже более веской: чек-лист — это
содержимое подсказок. Отдать его целиком значит выдать в контрольном режиме
то, чего там не должно быть вовсе, и обойти выдачу по одному пункту
(docs/product/MODES.md#подсказка-по-запросу). Подсказки идут только событием
`hint.shown` из живой сессии, эталонные вопросы — только в разборе.
"""

from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import audit, require
from app.domain import ekp
from app.db.base import get_session
from app.config import get_settings
from app.domain.roles import Role
from app.scenarios import store
from app.scenarios.editor import validate
from app.scenarios.generation import GenerationError, generate, generate_from_description
from app.dialog.llm import LlmUnavailable
from app.scenarios.loader import ScenarioError
from app.session.hub import hub

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])

HIDDEN_FROM_TRAINEE = {"facts", "ground_truth", "tree", "checklist"}


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


@router.post("/drafts/from-template", status_code=201)
async def create_template_draft(
    body: TemplateDraftIn, request: Request, db: AsyncSession | None = Depends(scenario_session)
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    source = store.get(body.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="published_source_not_found")
    row = await store.create_draft(db, source=source, title=body.title, owner_login=who.login)
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
        row = await store.create_draft(db, source=source, proposal=proposal, owner_login=who.login)
    except LlmUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"локальная модель недоступна: {exc}") from exc
    except GenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
            db, source=source, full_proposal=proposal, owner_login=who.login
        )
    except LlmUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"локальная модель недоступна: {exc}") from exc
    except GenerationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
        row = await store.update_draft(db, row, body)
    except ScenarioError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
        row = await store.revise_draft(db, row, proposal)
    except LlmUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"локальная модель недоступна: {exc}") from exc
    except (GenerationError, ScenarioError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit(who.login, who.role.value, "scenario.draft.ai_revise", row.id,
                body.comment.strip()[:500])
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


@router.post("/drafts/{scenario_id}/approve")
async def approve_draft(
    scenario_id: str, request: Request, db: AsyncSession | None = Depends(scenario_session)
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    row = await store.draft(db, scenario_id, owner_login=who.login)
    if row is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    try:
        scenario = await store.approve_draft(db, row)
    except ScenarioError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit(who.login, who.role.value, "scenario.approve", scenario.id)
    return {"id": scenario.id, "status": "published", "title": scenario.title}


@router.get("")
async def listing(
    request: Request, db: AsyncSession | None = Depends(scenario_session)
) -> list[dict]:
    who = require(request, Role.INSTRUCTOR, Role.ADMIN, Role.TRAINEE)
    owned_ids = (
        await store.owned_scenario_ids(db, who.login)
        if who is not None and who.role is Role.INSTRUCTOR
        else set()
    )
    return [
        {
            "id": scenario.id,
            "title": scenario.title,
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
        }
        for scenario in store.all_scenarios()
    ]


@router.delete("/{scenario_id}")
async def archive_scenario(
    scenario_id: str, request: Request,
    db: AsyncSession | None = Depends(scenario_session),
) -> dict:
    """Мягкое удаление: история занятий остаётся целой, сценарий можно вернуть."""
    who = require(request, Role.INSTRUCTOR)
    if hub.has_active_scenario(scenario_id):
        raise HTTPException(status_code=409, detail="scenario_is_used_by_active_session")
    scenario = await store.archive(db, scenario_id, owner_login=who.login)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario_not_found")
    await audit(who.login, who.role.value, "scenario.archive", scenario_id)
    return {"id": scenario_id, "status": "archived", "title": scenario.title}


@router.post("/{scenario_id}/restore")
async def restore_scenario(
    scenario_id: str, request: Request,
    db: AsyncSession | None = Depends(scenario_session),
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    scenario = await store.restore_archived(db, scenario_id, owner_login=who.login)
    if scenario is None:
        raise HTTPException(status_code=404, detail="archived_scenario_not_found")
    await audit(who.login, who.role.value, "scenario.restore", scenario_id)
    return {"id": scenario_id, "status": "published", "title": scenario.title}


@router.get("/{scenario_id}")
async def read(scenario_id: str, request: Request) -> dict:
    # Training content is local but not public: anonymous clients must not be
    # able to enumerate cards or inspect even the trainee-safe scenario body.
    require(request, Role.INSTRUCTOR, Role.ADMIN, Role.TRAINEE)
    scenario = store.get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario_not_found")
    payload = scenario.model_dump(mode="json")
    for key in HIDDEN_FROM_TRAINEE:
        payload.pop(key, None)
    payload["required_fields"] = scenario.required_fields
    return payload
