from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.ws import call
from app.dialog.slots import TurnResult
from app.domain.events import Exercise, TranscriptEntry


@pytest.mark.asyncio
async def test_text_dialogue_provider_error_does_not_log_prompt_or_provider_body(caplog, monkeypatch):
    secret = "private-incident-address-from-provider-error"

    class Caller:
        async def reply(self, *_args):
            raise RuntimeError(secret)

    class Slots:
        def hear(self, text):
            return TurnResult(text=text)

        def revealed_facts(self):
            return []

    monkeypatch.setattr(call.hub, "journal", None)
    monkeypatch.setattr(call.hub, "to_trainee", lambda *_args: None)
    monkeypatch.setattr(call.hub, "to_observers", lambda *_args: None)
    state = SimpleNamespace(
        ended=False,
        exercise=Exercise.CARD,
        dispatched_card=None,
        caller=Caller(),
        persona=object(),
        scenario=SimpleNamespace(facts=[], checklist=[]),
        slots=Slots(),
        text_revealed_facts={},
        append=lambda speaker, text: TranscriptEntry(
            ref="transcript-ref", speaker=speaker, text=text, at=datetime.now(timezone.utc),
        ),
    )

    await call._handle(uuid4(), state, SimpleNamespace(type="text.turn", text="where is the incident"))

    assert secret not in caplog.text
    assert "RuntimeError" in caplog.text
