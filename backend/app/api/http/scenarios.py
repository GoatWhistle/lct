"""Библиотека сценариев по HTTP.

`GET /api/scenarios/{id}` **не отдаёт** `facts` и `ground_truth`: иначе курсант
откроет DevTools и прочитает адрес до того, как его спросит.

`checklist` скрыт по той же причине и даже более веской: чек-лист — это
содержимое подсказок. Отдать его целиком значит выдать в контрольном режиме
то, чего там не должно быть вовсе, и обойти выдачу по одному пункту
(docs/product/MODES.md#подсказка-по-запросу). Подсказки идут только событием
`hint.shown` из живой сессии, эталонные вопросы — только в разборе.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import audit, require
from app.db.base import get_session
from app.domain.roles import Role
from app.scenarios import store
from app.scenarios.editor import validate
from app.scenarios.loader import ScenarioError

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])

HIDDEN_FROM_TRAINEE = {"facts", "ground_truth", "tree", "checklist"}


class TemplateDraftIn(BaseModel):
    source_id: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)


def _draft_out(row) -> dict:
    return {
        "id": row.id,
        "status": row.status,
        "generation": "template_copy",
        "body": row.body,
    }


@router.post("/drafts/from-template", status_code=201)
async def create_template_draft(
    body: TemplateDraftIn, request: Request, db: AsyncSession = Depends(get_session)
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    source = store.get(body.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="published_source_not_found")
    row = await store.create_draft(db, source=source, title=body.title)
    await audit(who.login, who.role.value, "scenario.draft.create", row.id, f"template:{source.id}")
    return _draft_out(row)


@router.get("/drafts/{scenario_id}")
async def read_draft(
    scenario_id: str, request: Request, db: AsyncSession = Depends(get_session)
) -> dict:
    require(request, Role.INSTRUCTOR)
    row = await store.draft(db, scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    return _draft_out(row)


@router.patch("/drafts/{scenario_id}")
async def patch_draft(
    scenario_id: str,
    body: dict[str, Any],
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    row = await store.draft(db, scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    try:
        row = await store.update_draft(db, row, body)
    except ScenarioError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit(who.login, who.role.value, "scenario.draft.update", row.id)
    return _draft_out(row)


@router.post("/drafts/{scenario_id}/validate")
async def validate_draft(
    scenario_id: str, request: Request, db: AsyncSession = Depends(get_session)
) -> dict:
    require(request, Role.INSTRUCTOR)
    row = await store.draft(db, scenario_id)
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
    scenario_id: str, request: Request, db: AsyncSession = Depends(get_session)
) -> dict:
    who = require(request, Role.INSTRUCTOR)
    row = await store.draft(db, scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail="draft_not_found")
    try:
        scenario = await store.approve_draft(db, row)
    except ScenarioError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit(who.login, who.role.value, "scenario.approve", scenario.id)
    return {"id": scenario.id, "status": "published", "title": scenario.title}


@router.get("")
async def listing() -> list[dict]:
    return [
        {
            "id": scenario.id,
            "title": scenario.title,
            "type": scenario.type.value,
            "level": scenario.level.value,
            "topics": scenario.topics,
            "modes": scenario.modes,
            "dds": scenario.ground_truth.dds.value if scenario.ground_truth.dds else None,
        }
        for scenario in store.all_scenarios()
    ]


@router.get("/{scenario_id}")
async def read(scenario_id: str) -> dict:
    scenario = store.get(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail="scenario_not_found")
    payload = scenario.model_dump(mode="json")
    for key in HIDDEN_FROM_TRAINEE:
        payload.pop(key, None)
    payload["required_fields"] = scenario.required_fields
    return payload
