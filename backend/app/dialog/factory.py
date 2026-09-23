"""Кто играет звонящего: локальная LLM, таблица или заготовки.

Провайдер и модель меняются значением в конфиге, а не кодом. Заготовки —
не запасной костыль, а рабочий режим: занятие идёт и без сети.
"""

import logging

from app.config import get_settings
from app.dialog.caller import Caller, LlmCaller, TemplateCaller
from app.dialog.llm import LlmClient
from app.dialog.llm import is_loopback_url
from app.dialog.tree import TreeCaller, has_table
from app.db.base import get_sessionmaker

log = logging.getLogger(__name__)


def build_caller(
    scenario_id: str | None = None,
    sessionmaker=None,
    *,
    use_pregenerated: bool = False,
) -> Caller:
    settings = get_settings()

    local = settings.llm_provider == "local"
    mode = settings.dialogue_model_mode
    if mode not in {"dialogue", "russian_control"}:
        raise ValueError(f"неизвестный DIALOGUE_MODEL_MODE: {mode}")
    model = settings.llm_model_control if mode == "russian_control" else settings.llm_model_caller
    base_url = settings.llm_control_base_url if mode == "russian_control" else settings.llm_base_url
    model_allowed = settings.llm_provider != "disabled" and bool(model and base_url and (
        is_loopback_url(base_url, allow_docker_host=settings.allow_docker_host_models)
        if settings.offline or local else settings.llm_api_key
    ))

    # Локальный loopback не считается внешней сетью. Если сервер модели не
    # отвечает, LlmCaller откатится на проверенные заготовки этой же реплики.
    if model_allowed:
        client = LlmClient(sessionmaker=sessionmaker or _safe_sessionmaker(), base_url=base_url)
        log.info("звонящий: режим %s, модель %s, адрес %s", mode, model, base_url)
        return LlmCaller(client, model=model)

    if use_pregenerated and scenario_id and has_table(scenario_id):
        log.info("звонящий по предгенерированной таблице сценария %s", scenario_id)
        return TreeCaller(scenario_id)

    log.info("звонящий отвечает фактами сценария: модель отключена или предгенерация не разрешена")
    return TemplateCaller()


def _safe_sessionmaker():
    """Кэш в Postgres — приятный бонус, а не условие работы звонящего."""
    try:
        return get_sessionmaker()
    except Exception:  # noqa: BLE001
        log.warning("кэш LLM выключен: база недоступна")
        return None
