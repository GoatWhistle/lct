"""Кто играет звонящего: LLM, если есть ключ, иначе заготовки.

Провайдер и модель меняются значением в конфиге, а не кодом. Заготовки —
не запасной костыль, а рабочий режим: занятие идёт и без сети.
"""

import logging

from app.config import get_settings
from app.dialog.caller import Caller, LlmCaller, TemplateCaller
from app.dialog.llm import LlmClient
from app.dialog.tree import TreeCaller, has_table
from app.db.base import get_sessionmaker

log = logging.getLogger(__name__)


def build_caller(scenario_id: str | None = None, sessionmaker=None) -> Caller:
    settings = get_settings()

    # Офлайн и «нет ключа» — это один и тот же путь: предгенерированная таблица,
    # а не локальная модель. Формулировки в ней от облачной модели, а задержка
    # нулевая (docs/arch/STACK.md).
    if (settings.offline or not settings.llm_api_key) and scenario_id and has_table(scenario_id):
        log.info("звонящий по предгенерированной таблице сценария %s", scenario_id)
        return TreeCaller(scenario_id)

    if not settings.llm_api_key or settings.offline:
        reason = "офлайн-режим" if settings.offline else "нет ключа LLM"
        log.info("звонящий отвечает заготовками: %s (таблицы нет — make pregen)", reason)
        return TemplateCaller()

    client = LlmClient(sessionmaker=sessionmaker or _safe_sessionmaker())
    log.info("звонящий на модели %s", settings.llm_model_caller)
    return LlmCaller(client, model=settings.llm_model_caller)


def _safe_sessionmaker():
    """Кэш в Postgres — приятный бонус, а не условие работы звонящего."""
    try:
        return get_sessionmaker()
    except Exception:  # noqa: BLE001
        log.warning("кэш LLM выключен: база недоступна")
        return None
