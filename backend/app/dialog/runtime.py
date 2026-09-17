"""Модели диалога, общие для всех сессий процесса.

Эмбеддер грузится один раз при старте приложения: 2–3 секунды на загрузке
занятия недопустимы, а на старте стенда их никто не заметит.
Если модели нет — занятие всё равно идёт, но подсказки теряют точность
(идут по порядку чек-листа), и это видно в /api/health.
"""

import logging
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.dialog.embeddings import E5Embedder, Embedder

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder | None:
    model_dir = ROOT / get_settings().models_dir / "e5-small"
    try:
        return E5Embedder(model_dir)
    except FileNotFoundError:
        log.warning("модели эмбеддингов нет в %s — слот-автомат выключен, make models", model_dir)
        return None
