"""Клиент совместимого API для локальных или внешних моделей.

Кэш ответов по хешу контекста лежит в Postgres, а не в Redis: база уже поднята,
лишняя движущаяся часть на стенде не нужна (docs/arch/STACK.md). Кэш работает
и онлайн — экономия и ускорение повторов, — и как накопитель материала
для офлайн-дерева.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db.models import LlmCache

log = logging.getLogger(__name__)


class LlmUnavailable(RuntimeError):
    """Сеть, ключ или провайдер отказали. Звонящий откатывается на заготовки,
    занятие продолжается — молчащий звонящий хуже шаблонной фразы."""


def is_loopback_url(value: str, *, allow_docker_host: bool = False) -> bool:
    """В офлайн-режиме модели разрешены лишь на той же машине.

    Не доверяем доменам или hosts-записям: они могут указывать наружу.
    """
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        try:
            local_host = ip_address(host).is_loopback
        except ValueError:
            local_host = allow_docker_host and host == "host.docker.internal"
        return (url.scheme == "http" and local_host and url.port is not None
                and not url.username and not url.password)
    except (ValueError, TypeError):
        return False


def _spoken_content(raw: str, *, strip_reasoning: bool = False) -> str:
    """Убрать только пустой служебный хвост Qwen3, не рассуждения модели.

    llama.cpp с выключенным thinking иногда возвращает в `content` один
    закрывающий `</think>` перед самой репликой. Внутренний текст размышлений
    мы намеренно не вырезаем: если он есть, ответ небезопасен и идёт fallback.
    """
    text = raw.strip()
    if strip_reasoning:
        closing = list(re.finditer(r"</think>\s*", text, re.IGNORECASE))
        if closing:
            text = text[closing[-1].end():].strip()
        elif text.startswith("<|"):
            start = text.find("{")
            if start >= 0:
                text = text[start:].strip()
    text = re.sub(r"^(?:</think>\s*)+", "", text, flags=re.IGNORECASE).strip()
    if re.search(r"</?think\b", text, re.IGNORECASE) or "<|" in text:
        raise LlmUnavailable("ответ содержит служебные токены модели")
    if not text:
        raise LlmUnavailable("пустой ответ модели: весь бюджет токенов ушёл в рассуждение")
    return text


@dataclass
class LlmRequest:
    messages: list[dict]
    model: str
    temperature: float = 0.8
    # С запасом на рассуждающие модели: Qwen3 тратит на размышление сотни токенов
    # и при малом бюджете возвращает пустой ответ с finish_reason="length".
    max_tokens: int = 400
    response_format: dict | None = None
    # Только для внутренних структурированных задач. В репликах звонящего
    # рассуждение всегда отвергается, чтобы оно не попало в эфир.
    strip_reasoning: bool = False

    def cache_key(self) -> str:
        payload = json.dumps(
            {"m": self.model, "t": self.temperature, "msgs": self.messages,
             "format": self.response_format, "strip_reasoning": self.strip_reasoning},
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
        base_url: str | None = None,
        # Ответ дольше этого бессмысленен: бюджет хода — 1.5 с, а звонящий
        # с заготовками ответит сразу.
        timeout: float = 8.0,
    ) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.llm_base_url).rstrip("/")
        self._key = settings.llm_api_key
        self._local_only = settings.offline or settings.llm_provider == "local"
        self._allow_docker_host = settings.allow_docker_host_models
        self._sessionmaker = sessionmaker
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport, trust_env=False)

    @property
    def configured(self) -> bool:
        if self._local_only:
            return is_loopback_url(
                self._base_url, allow_docker_host=self._allow_docker_host
            )
        return bool(self._key and self._base_url)

    async def complete(self, request: LlmRequest, *, use_cache: bool = True) -> str:
        """Ответ модели. Кэш по хешу контекста: та же реплика на том же месте
        занятия звучит одинаково у каждой группы."""
        if not self.configured:
            raise LlmUnavailable("локальный адрес модели недопустим или провайдер не настроен")

        key = request.cache_key()
        if use_cache:
            cached = await self._from_cache(key)
            if cached is not None:
                return _spoken_content(cached, strip_reasoning=request.strip_reasoning)

        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._key}"} if self._key else {},
                json={
                    "model": request.model,
                    "messages": request.messages,
                    "temperature": request.temperature,
                    "max_tokens": request.max_tokens,
                    # Рассуждение в ответе не нужно: оно только раздувает трафик.
                    # Провайдеры, которые про это поле не знают, его игнорируют.
                    "reasoning": {"exclude": True},
                    **({"response_format": request.response_format}
                       if request.response_format is not None else {}),
                },
            )
        except httpx.HTTPError as exc:
            raise LlmUnavailable(f"{type(exc).__name__}") from exc

        if response.status_code != 200:
            # Тело ошибки в лог, ключ в заголовке — не логируется.
            raise LlmUnavailable(f"HTTP {response.status_code}: {response.text[:200]}")

        try:
            message = response.json()["choices"][0]["message"]
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise TypeError("content не строка")
            text = _spoken_content(content or "", strip_reasoning=request.strip_reasoning)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LlmUnavailable("некорректный ответ локальной модели") from exc
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
