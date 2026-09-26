"""Student-authored case outlines and instructor moderation."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import Principal, add_audit_entry, audit, require
from app.config import get_settings
from app.db.base import get_session
from app.db.models import Group, ScenarioSubmission, Trainee
from app.db.models import Scenario as ScenarioRow
from app.domain.classifiers import IncidentType, Level
from app.domain.kio import KIO, derive_incident
from app.domain.roles import Role
from app.scenarios import store
from app.scenarios.editor import validate
from app.scenarios.loader import ScenarioError

router = APIRouter(prefix="/api/scenario-submissions", tags=["scenario submissions"])
_demo_submissions: dict[UUID, dict] = {}


def _card_address(card: KIO) -> str:
    explicit = (card.address or "").strip()
    fallback = " ".join(filter(None, (card.street, card.building))).strip()
    return explicit or fallback


async def submission_session() -> AsyncIterator[AsyncSession | None]:
    if get_settings().demo_no_db:
        yield None
    else:
        async for db in get_session():
            yield db


class SubmissionIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    level: Level
    kio: KIO

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 3:
            raise ValueError("title must contain at least three non-space characters")
        return normalized

    @model_validator(mode="after")
    def validate_kio(self):
        card = derive_incident(self.kio)
        if card.incident_type is None or len((card.description or "").strip()) < 20:
            raise ValueError("KIO needs incident type and a meaningful description")
        if not _card_address(card):
            raise ValueError("KIO needs a usable address")
        if card.incident_group is None or not card.signs:
            raise ValueError("KIO needs a classifier group and signs")
        if not card.notify:
            raise ValueError("KIO needs at least one derived DDS recipient")
        data = card.model_dump()
        data.update(
            {
                "card_id": uuid4(),
                "registered_at": None,
                "response_status": "registered",
                "caller_number": None,
                "incident_code": None,
                "notify": [],
                "dispatch_order_at": None,
                "arrival_at": None,
            }
        )
        object.__setattr__(self, "kio", derive_incident(KIO.model_validate(data)))
        return self


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def rejection_needs_reason(self):
        if self.decision == "reject" and not self.comment.strip():
            raise ValueError("comment is required when rejecting a proposal")
        return self


def _out(row, author_name: str | None = None) -> dict:
    def get(name, default=None):
        if isinstance(row, dict):
            return row.get(name, default)
        return getattr(row, name, default)

    return {
        "id": str(get("id")),
        "author_name": author_name or get("author_name", "Курсант"),
        "title": get("title"),
        "incident_type": get("incident_type"),
        "level": get("level"),
        "description": get("description"),
        "address": get("address", ""),
        "victims": get("victims"),
        "kio": get("kio"),
        "status": get("status"),
        "review_comment": get("review_comment", ""),
        "scenario_id": get("scenario_id"),
        "created_at": get("created_at"),
        "reviewed_at": get("reviewed_at"),
    }


def _scenario_for(row) -> object:
    sid = f"student-{row['id'].hex if isinstance(row, dict) else row.id.hex}"
    title = row["title"] if isinstance(row, dict) else row.title
    level = row["level"] if isinstance(row, dict) else row.level
    kio_data = row.get("kio") if isinstance(row, dict) else row.kio
    if kio_data:
        card = derive_incident(KIO.model_validate(kio_data))
        if card.incident_type is None:
            raise ScenarioError("КИО не содержит тип происшествия")
        address = _card_address(card) or None
        facts = [{"id": "event", "value": card.description or title}]
        if address:
            facts.append({"id": "address", "value": address})
        caller = "; ".join(
            filter(None, (card.caller_name, card.caller_contact, card.phone_on_scene))
        )
        if caller:
            facts.append({"id": "f_caller", "value": caller})
        raw = {
            "id": sid,
            "title": title.strip(),
            "type": card.incident_type.value,
            "level": level,
            "topics": ["student-created", "moderated-kio"],
            "modes": ["training", "exam"],
            "persona": {"base": "Утверждённая преподавателем учебная карточка КИО."},
            "first_line": card.description or title,
            "signs": card.signs,
            "facts": facts,
            "checklist": [
                {"id": "q_event", "question": "Что произошло?", "fact": "event"}
            ],
            "required_fields": ["address", "description"],
            "outcome": "card",
            "dds_decision": {"expected": "accept"},
            "ground_truth": {
                **({"address": address} if address else {}),
                **(
                    {"victims": card.victims_count}
                    if card.victims_count is not None
                    else {}
                ),
            },
            "student_card": card.model_dump(mode="json"),
        }
        return validate(raw)

    incident_type = row["incident_type"] if isinstance(row, dict) else row.incident_type
    description = row["description"] if isinstance(row, dict) else row.description
    address = row.get("address", "") if isinstance(row, dict) else row.address
    victims = row.get("victims") if isinstance(row, dict) else row.victims
    facts = [{"id": "event", "value": description.strip()}]
    if address and address.strip():
        facts.append({"id": "address", "value": address.strip()})
    raw = {
        "id": sid,
        "title": title.strip(),
        "type": incident_type,
        "level": level,
        "topics": ["student-created"],
        "modes": ["training", "exam"],
        "persona": {
            "base": "Авторский учебный сюжет курсанта, проверенный преподавателем."
        },
        "first_line": description.strip(),
        "facts": facts,
        "checklist": [{"id": "q_event", "question": "Что произошло?", "fact": "event"}],
        "outcome": "card",
        "ground_truth": {
            **({"address": address.strip()} if address and address.strip() else {}),
            **({"victims": victims} if victims is not None else {}),
        },
    }
    return validate(raw)


@router.post("", status_code=201)
async def create_submission(
    body: SubmissionIn,
    request: Request,
    db: AsyncSession | None = Depends(submission_session),
) -> dict:
    who: Principal = require(request, Role.TRAINEE)
    if who.trainee_id is None:
        raise HTTPException(status_code=403, detail="trainee_profile_required")
    now = datetime.now(UTC)
    card = body.kio
    incident_type = card.incident_type
    description = card.description or ""
    address = _card_address(card)
    victims = card.victims_count
    if db is None:
        row = {
            "id": uuid4(),
            "author_trainee_id": who.trainee_id,
            "author_name": who.full_name,
            "group_id": None,
            "title": body.title,
            "level": body.level.value,
            "kio": card.model_dump(mode="json"),
            "incident_type": incident_type.value,
            "description": description,
            "address": address,
            "victims": victims,
            "status": "pending",
            "review_comment": "",
            "reviewed_by": None,
            "scenario_id": None,
            "created_at": now,
            "reviewed_at": None,
        }
        _demo_submissions[row["id"]] = row
    else:
        trainee = await db.get(Trainee, who.trainee_id)
        if trainee is None:
            raise HTTPException(status_code=403, detail="trainee_profile_required")
        if trainee.group_id is None:
            raise HTTPException(
                status_code=409, detail="trainee_group_required_for_review"
            )
        group = await db.get(Group, trainee.group_id)
        if group is None or group.owner_login is None:
            raise HTTPException(
                status_code=409, detail="instructor_group_required_for_review"
            )
        row = ScenarioSubmission(
            id=uuid4(),
            author_trainee_id=trainee.id,
            group_id=trainee.group_id,
            title=body.title.strip(),
            incident_type=incident_type.value,
            level=body.level.value,
            description=description,
            address=address,
            victims=victims,
            kio=card.model_dump(mode="json"),
        )
        db.add(row)
        add_audit_entry(
            db, who.login, who.role.value, "scenario.submission.create", str(row.id)
        )
        await db.commit()
    if isinstance(row, dict):
        await audit(who.login, who.role.value, "scenario.submission.create", str(row["id"]))
    return _out(row, who.full_name)


@router.get("")
async def list_submissions(
    request: Request,
    db: AsyncSession | None = Depends(submission_session),
) -> list[dict]:
    who: Principal = require(request, Role.TRAINEE, Role.INSTRUCTOR, Role.ADMIN)
    if db is None:
        if who.role is Role.TRAINEE:
            rows = [
                row
                for row in _demo_submissions.values()
                if row["author_trainee_id"] == who.trainee_id
            ]
        else:
            rows = list(_demo_submissions.values())
        rows.sort(key=lambda item: item["created_at"], reverse=True)
        return [_out(row) for row in rows]

    query = select(ScenarioSubmission, Trainee.name).join(
        Trainee, Trainee.id == ScenarioSubmission.author_trainee_id
    )
    if who.role is Role.TRAINEE:
        if who.trainee_id is None:
            raise HTTPException(status_code=403, detail="trainee_profile_required")
        query = query.where(ScenarioSubmission.author_trainee_id == who.trainee_id)
    elif who.role is Role.INSTRUCTOR:
        owned_groups = select(Group.id).where(Group.owner_login == who.login)
        query = query.where(ScenarioSubmission.group_id.in_(owned_groups))
    rows = (
        await db.execute(query.order_by(ScenarioSubmission.created_at.desc()))
    ).all()
    return [_out(row, name) for row, name in rows]


async def _reviewable(
    db: AsyncSession, submission_id: UUID, who: Principal
) -> ScenarioSubmission | None:
    # Serialize concurrent teacher decisions. Under PostgreSQL READ COMMITTED,
    # a second reviewer waits and then observes the committed non-pending status,
    # instead of racing to publish the same scenario twice.
    query = (
        select(ScenarioSubmission)
        .where(ScenarioSubmission.id == submission_id)
        .with_for_update()
    )
    if who.role is Role.INSTRUCTOR:
        owned_groups = select(Group.id).where(Group.owner_login == who.login)
        query = query.where(ScenarioSubmission.group_id.in_(owned_groups))
    return await db.scalar(query)


@router.post("/{submission_id}/review")
async def review_submission(
    submission_id: UUID,
    body: ReviewIn,
    request: Request,
    db: AsyncSession | None = Depends(submission_session),
) -> dict:
    who: Principal = require(request, Role.INSTRUCTOR, Role.ADMIN)
    if db is None:
        row = _demo_submissions.get(submission_id)
        if row is None:
            raise HTTPException(status_code=404, detail="submission_not_found")
        if row["status"] != "pending":
            raise HTTPException(status_code=409, detail="submission_already_reviewed")
    else:
        row = await _reviewable(db, submission_id, who)
        if row is None:
            raise HTTPException(status_code=404, detail="submission_not_found")
        if row.status != "pending":
            raise HTTPException(status_code=409, detail="submission_already_reviewed")

    scenario = None
    if body.decision == "approve":
        try:
            scenario = _scenario_for(row)
        except ScenarioError as exc:
            raise HTTPException(
                status_code=422, detail=f"scenario_invalid: {exc}"
            ) from exc

    now = datetime.now(UTC)
    if isinstance(row, dict):
        row["status"] = "approved" if scenario else "rejected"
        row["review_comment"] = body.comment.strip()
        row["reviewed_by"] = who.login
        row["reviewed_at"] = now
        if scenario is not None:
            row["scenario_id"] = scenario.id
    else:
        row.status = "approved" if scenario else "rejected"
        row.review_comment = body.comment.strip()
        row.reviewed_by = who.login
        row.reviewed_at = now
        if scenario is not None:
            db.add(
                ScenarioRow(
                    id=scenario.id,
                    title=scenario.title,
                    incident_type=scenario.type.value,
                    level=scenario.level.value,
                    topics=scenario.topics,
                    modes=scenario.modes,
                    status="published",
                    owner_login=who.login,
                    body=scenario.model_dump(mode="json"),
                )
            )
            row.scenario_id = scenario.id
        add_audit_entry(
            db,
            who.login,
            who.role.value,
            f"scenario.submission.{body.decision}",
            str(submission_id),
            f"comment_chars={len(body.comment.strip())}" if body.comment else "",
        )
        await db.commit()

    if scenario is not None:
        store.register_owned_scenario(scenario, who.login)
    if isinstance(row, dict):
        await audit(
            who.login,
            who.role.value,
            f"scenario.submission.{body.decision}",
            str(submission_id),
            f"comment_chars={len(body.comment.strip())}" if body.comment else "",
        )
    result = _out(row)
    if scenario is not None:
        result["scenario_id"] = scenario.id
    return result


def reset_demo_submissions() -> None:
    _demo_submissions.clear()
