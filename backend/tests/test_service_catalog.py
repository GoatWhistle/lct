"""Каталог служб из «СЛУЖБЫ 112» (lct-38).

Каталог только расширяет ручной выбор служб в карточке. Список оповещения
по-прежнему считается по классификатору, эталоны сценариев от каталога не зависят.
"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.ws import call as call_ws
from app.domain import ekp
from app.domain.events import ErrorEvent, KioPatchIn, SessionMode
from app.domain.kio import EDITABLE_KIO_FIELDS, KIO, apply_patch
from app.main import app
from app.scenarios.loader import load_file
from app.scoring.card import evaluate_card
from app.session.hub import hub
from app.session.state import SessionState
from app.session.store import MemorySessionStore


def test_catalog_keeps_all_209_services_with_attribution_and_source():
    data = ekp.catalog()

    assert len(data.services) == 209
    assert len({item.name for item in data.services}) == 209
    assert sum(item.service_level == "ДДС" for item in data.services) == 157
    assert "ded466988f505082922aacac9fff32b777d1279a" in data.transcription
    assert data.license.startswith("MIT")
    assert all(item.source_reference.startswith("СЛУЖБЫ 112.docx#word/media/image")
               for item in data.services)


@pytest.mark.parametrize("name", [
    'Служба 101 (ГУ МЧС России по г.Москве, ГКУ "Пожарно спасательный центр" ОДС)',
    'Служба 104 (АО "МОСГАЗ" Диспетчерское управление)',
    "ЦЭМП",
    'ЦОДД (ГКУ "Центр организации дорожного движения")',
    'ЭВАЖД (ГБУ "Учреждение по эксплуатации высотных административных и жилых домов")',
])
def test_rows_checked_against_our_screenshots_are_verbatim(name):
    assert name in ekp.catalog_names()


def test_classifier_services_and_notify_lists_are_untouched():
    """Индексы служб классификатора — основа списков оповещения 96 сценариев."""
    raw = json.loads(ekp.DATA.read_text(encoding="utf-8"))
    assert [item.name for item in ekp.services()] == [item["name"] for item in raw["services"]]
    assert len(ekp.services()) == 61


def test_operator_adds_catalog_services_without_changing_the_notify_list():
    assert "notify_extra" in EDITABLE_KIO_FIELDS
    card = KIO(notify=["Служба 101"])

    updated = apply_patch(card, {"notify_extra": ["ФСБ", "ЦЭМП", "ФСБ"], "notify": []})

    assert updated.notify_extra == ["ФСБ", "ЦЭМП"]
    assert updated.notify == ["Служба 101"], "автоматический список оператор не правит"
    with pytest.raises(ValidationError):
        apply_patch(card, {"notify_extra": ["Служба, которой нет"]})


def test_added_services_do_not_change_the_score_against_the_reference():
    library = Path(__file__).resolve().parents[2] / "scenarios"
    scenario = load_file(library / "fire-apartment-l2.yaml", library)
    card = KIO(address="улица Ленина, 14", description="горит балкон")

    plain = evaluate_card(scenario, card)
    extended = evaluate_card(scenario, card.model_copy(update={"notify_extra": ["ФСБ"]}))

    assert [(m.key, m.passed) for m in plain.metrics] == [(m.key, m.passed) for m in extended.metrics]
    assert plain.score == extended.score


def test_catalog_is_served_like_the_classifier():
    with TestClient(app) as client:
        response = client.get("/api/ekp/services")

    assert response.status_code == 200
    body = response.json()
    assert len(body["services"]) == 209
    assert {"id", "name", "organization", "service_level", "source_reference"} <= body["services"][0].keys()


def test_unknown_service_in_kio_patch_is_refused_without_failing_the_lesson(monkeypatch):
    sent = []
    monkeypatch.setattr(hub, "store", MemorySessionStore())
    monkeypatch.setattr(hub, "to_trainee", lambda _session_id, event: sent.append(event))
    monkeypatch.setattr(hub, "to_observers", lambda *_args: None)
    state = SessionState(session_id=uuid4(), scenario_id="test", scenario_title="Тест",
                         level="L1", mode=SessionMode.TRAINING)
    hub.register(state)

    async def patch(fields):
        async with hub.operation(state.session_id):
            await call_ws._handle(state.session_id, state, KioPatchIn(fields=fields))

    asyncio.run(patch({"notify_extra": ["Служба, которой нет"]}))
    assert isinstance(sent[-1], ErrorEvent) and "не принято" in sent[-1].message
    assert state.kio.notify_extra == []

    asyncio.run(patch({"notify_extra": ["ФСБ"]}))
    assert state.kio.notify_extra == ["ФСБ"]
