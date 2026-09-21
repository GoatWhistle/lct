"""Живые проверки LLM: клиент, кэш и звонящий своими словами.

Читают `backend/.env.test` — бесплатная модель через OpenRouter. Без этого файла
или без сети тест пропускается: обычные тесты в сеть не ходят вовсе.

Запускать отдельно (`make test-llm`): бесплатная рассуждающая модель отвечает
десятки секунд, и в общем прогоне ей не место.
"""

import os
from pathlib import Path

import pytest

ENV_TEST = Path(__file__).resolve().parents[1] / ".env.test"


def _load_test_env() -> bool:
    if not ENV_TEST.exists():
        return False
    for line in ENV_TEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ[key.strip()] = value.strip()
    from app.config import get_settings

    get_settings.cache_clear()
    return True


pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(not _load_test_env(), reason="нет backend/.env.test — живые проверки LLM пропущены"),
]


@pytest.fixture
async def client():
    from app.dialog.llm import LlmClient

    # Бесплатная рассуждающая модель думает по минуте: в живой проверке
    # это допустимо, в занятии — нет, там таймаут 8 секунд и откат на заготовки.
    llm = LlmClient(timeout=180)
    yield llm
    await llm.aclose()


async def test_provider_answers(client):
    from app.dialog.llm import LlmRequest, LlmUnavailable

    request = LlmRequest(
        messages=[{"role": "user", "content": "Ответь одним словом: работает"}],
        model=os.environ["LLM_MODEL_CALLER"],
        temperature=0,
        max_tokens=400,
    )
    try:
        text = await client.complete(request, use_cache=False)
    except LlmUnavailable as exc:
        pytest.skip(f"провайдер недоступен: {exc}")
    assert text, "пустой ответ модели"


async def test_caller_speaks_only_revealed_facts(client):
    """Главное свойство продукта не должно зависеть от послушности модели:
    в промпт попадают только раскрытые факты."""
    import numpy as np

    from app.dialog.caller import LlmCaller
    from app.dialog.llm import LlmUnavailable
    from app.dialog.persona import PersonaState
    from app.dialog.slots import SlotMachine
    from tests.test_slots import SCENARIO, StemEmbedder

    slots = SlotMachine(SCENARIO, StemEmbedder(), floor=0.5)
    persona = PersonaState(SCENARIO.persona)
    caller = LlmCaller(client, model=os.environ["LLM_MODEL_CALLER"])

    try:
        # Оператор спрашивает не об адресе — адрес прозвучать не должен.
        reply = await caller.reply(slots.hear("Что у вас случилось?"), persona, slots)
    except LlmUnavailable as exc:
        pytest.skip(f"провайдер недоступен: {exc}")

    assert caller.fallbacks == 0, "ответила не модель, а заготовка"
    assert "Ленина" not in reply.text, f"звонящий выдал адрес без вопроса: «{reply.text}»"
    assert len(reply.text) < 300, "звонящий пишет объяснительную вместо крика"


async def test_same_context_comes_from_cache(client):
    """Кэш по хешу контекста: та же реплика на том же месте занятия звучит
    одинаково у каждой группы и не стоит второго запроса."""
    from app.dialog.llm import LlmRequest, LlmUnavailable

    request = LlmRequest(
        messages=[{"role": "user", "content": "Назови одно слово: пожар"}],
        model=os.environ["LLM_MODEL_CALLER"],
        temperature=0,
        max_tokens=400,
    )
    try:
        first = await client.complete(request)
    except LlmUnavailable as exc:
        pytest.skip(f"провайдер недоступен: {exc}")

    import time

    started = time.monotonic()
    second = await client.complete(request)
    elapsed = time.monotonic() - started

    if client._sessionmaker is None:
        pytest.skip("кэш выключен: база недоступна")
    assert second == first, "кэш вернул другой ответ"
    assert elapsed < 1.0, f"второй запрос занял {elapsed:.2f} с — кэш не сработал"
