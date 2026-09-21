"""Клиент облачной LLM за интерфейсом: провайдер меняется значением в конфиге.

Кэш ответов по хешу контекста лежит в Postgres, а не в Redis: база уже поднята,
лишняя движущаяся часть на стенде не нужна (docs/arch/STACK.md). Кэш работает
и онлайн — экономия и ускорение повторов, — и как накопитель материала
для офлайн-дерева.
"""

import hashlib
import json
import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db.models import LlmCache

log = logging.getLogger(__name__)


class LlmUnavailable(RuntimeError):
    """Сеть, ключ или провайдер отказали. Звонящий откатывается на заготовки,
    занятие продолжается — молчащий звонящий хуже шаблонной фразы."""


@dataclass
class LlmRequest:
    messages: list[dict]
    model: str
    temperature: float = 0.8
    # С запасом на рассуждающие модели: Qwen3 тратит на размышление сотни токенов
    # и при малом бюджете возвращает пустой ответ с finish_reason="length".
    max_tokens: int = 400

    def cache_key(self) -> str:
        payload = json.dumps(
            {"m": self.model, "t": self.temperature, "msgs": self.messages},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class LlmClient:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        # Ответ дольше этого бессмысленен: бюджет хода — 1.5 с, а звонящий
        # с заготовками ответит сразу.
        timeout: float = 8.0,
    ) -> None:
        settings = get_settings()
        self._base_url = settings.llm_base_url.rstrip("/")
        self._key = settings.llm_api_key
        self._sessionmaker = sessionmaker
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self._key and self._base_url)

    async def complete(self, request: LlmRequest, *, use_cache: bool = True) -> str:
        """Ответ модели. Кэш по хешу контекста: та же реплика на том же месте
        занятия звучит одинаково у каждой группы."""
        if not self.configured:
            raise LlmUnavailable("не задан ключ или адрес провайдера")

        key = request.cache_key()
        if use_cache:
            cached = await self._from_cache(key)
            if cached is not None:
                return cached

        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._key}"},
                json={
                    "model": request.model,
                    "messages": request.messages,
                    "temperature": request.temperature,
                    "max_tokens": request.max_tokens,
                    # Рассуждение в ответе не нужно: оно только раздувает трафик.
                    # Провайдеры, которые про это поле не знают, его игнорируют.
                    "reasoning": {"exclude": True},
                },
            )
        except httpx.HTTPError as exc:
            raise LlmUnavailable(f"{type(exc).__name__}") from exc

        if response.status_code != 200:
            # Тело ошибки в лог, ключ в заголовке — не логируется.
            raise LlmUnavailable(f"HTTP {response.status_code}: {response.text[:200]}")

        message = response.json()["choices"][0]["message"]
        text = (message.get("content") or "").strip()
        if not text:
            # У рассуждающих моделей при нехватке бюджета весь ответ уходит
            # в размышление, а content приходит пустым. Для занятия это отказ:
            # звонящий откатится на заготовку, а не промолчит.
            raise LlmUnavailable("пустой ответ модели: весь бюджет токенов ушёл в рассуждение")
        if use_cache and text:
            await self._to_cache(key, request, text)
        return text

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── кэш ──

    async def _from_cache(self, key: str) -> str | None:
        if self._sessionmaker is None:
            return None
        try:
            async with self._sessionmaker() as db:
                return await db.scalar(
                    select(LlmCache.response).where(LlmCache.context_hash == key)
                )
        except Exception:  # noqa: BLE001 — без кэша занятие идёт, без базы тоже
            log.exception("кэш LLM: чтение не удалось")
            return None

    async def _to_cache(self, key: str, request: LlmRequest, text: str) -> None:
        if self._sessionmaker is None:
            return
        try:
            async with self._sessionmaker() as db:
                db.add(
                    LlmCache(
                        context_hash=key,
                        model=request.model,
                        prompt=json.dumps(request.messages, ensure_ascii=False)[:8000],
                        response=text,
                    )
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            log.exception("кэш LLM: запись не удалась")
