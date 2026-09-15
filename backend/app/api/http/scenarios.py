"""Библиотека сценариев по HTTP.

`GET /api/scenarios/{id}` **не отдаёт** `facts` и `ground_truth`: иначе курсант
откроет DevTools и прочитает адрес до того, как его спросит.

`checklist` скрыт по той же причине и даже более веской: чек-лист — это
содержимое подсказок. Отдать его целиком значит выдать в контрольном режиме
то, чего там не должно быть вовсе, и обойти выдачу по одному пункту
(docs/product/MODES.md#подсказка-по-запросу). Подсказки идут только событием
`hint.shown` из живой сессии, эталонные вопросы — только в разборе.
"""

from fastapi import APIRouter, HTTPException

from app.scenarios import store

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])

HIDDEN_FROM_TRAINEE = {"facts", "ground_truth", "tree", "checklist"}


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
