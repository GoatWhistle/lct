"""Живые проверки локальной LLM: клиент, кэш и ответы звонящего.

Читают `backend/.env.test` с `LLM_PROVIDER=local` и loopback URL. Запускайте
после `make local-llm` отдельной командой `make test-llm-local`. Сеть не нужна;
основной pytest намеренно исключает медленный инференс.
"""

import os
from pathlib import Path

import pytest

ENV_TEST = Path(__file__).resolve().parents[1] / ".env.test"


def _read_test_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_TEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


HAS_TEST_ENV = ENV_TEST.is_file()
pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(not HAS_TEST_ENV, reason="нет backend/.env.test — живые проверки LLM пропущены"),
]


@pytest.fixture(autouse=True)
def local_llm_test_environment():
    """Изолировать live-конфиг: collection обычных тестов не меняет env."""
    if not HAS_TEST_ENV:
        yield
        return

    values = _read_test_env()
    original = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        os.environ[key] = value

    from app.config import get_settings
    from app.dialog.llm import is_loopback_url

    get_settings.cache_clear()
    try:
        settings = get_settings()
        if (
            settings.llm_provider != "local"
            or not is_loopback_url(settings.llm_base_url)
            or settings.llm_api_key
        ):
            raise pytest.UsageError(
                "test-llm-local требует LLM_PROVIDER=local, loopback URL и пустой LLM_API_KEY"
            )
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


@pytest.fixture
async def client():
    from app.dialog.llm import LlmClient

    # Локальная модель может быть медленной на слабом CPU; в занятии таймаут
    # короче, и при отказе используются проверенные заготовки.
    llm = LlmClient(timeout=180)
    yield llm
    await llm.aclose()


@pytest.fixture
async def database_client(postgres_access):
    """Подключить проверку кэша к PostgreSQL, когда тестовый стенд её дал.

    Живые inference smoke должны работать и без БД, но проверка устойчивого
    кэша имеет смысл только в отдельной DB-интеграционной цели.
    """
    from sqlalchemy import select

    from app.db.base import get_sessionmaker
    from app.db.models import LlmCache
    from app.dialog.llm import LlmClient

    sessionmaker = get_sessionmaker()
    try:
        async with sessionmaker() as db:
            await db.scalar(select(LlmCache.context_hash).limit(1))
    except Exception as exc:  # noqa: BLE001 — кэш необязателен для обычного inference smoke
        if os.environ.get("DATABASE_URL"):
            raise
        pytest.skip(f"таблица кэша LLM недоступна: {type(exc).__name__}")

    db_client = LlmClient(sessionmaker=sessionmaker, timeout=180)
    yield db_client
    await db_client.aclose()


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
        pytest.fail(f"локальная модель недоступна: {exc}")
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
        pytest.fail(f"локальная модель недоступна: {exc}")

    assert caller.fallbacks == 0, "ответила не модель, а заготовка"
    assert "Ленина" not in reply.text, f"звонящий выдал адрес без вопроса: «{reply.text}»"
    assert len(reply.text) < 300, "звонящий пишет объяснительную вместо крика"


async def test_same_context_comes_from_cache(database_client):
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
        first = await database_client.complete(request)
    except LlmUnavailable as exc:
        pytest.fail(f"локальная модель недоступна: {exc}")

    import time

    started = time.monotonic()
    second = await database_client.complete(request)
    elapsed = time.monotonic() - started

    assert second == first, "кэш вернул другой ответ"
    assert elapsed < 1.0, f"второй запрос занял {elapsed:.2f} с — кэш не сработал"
