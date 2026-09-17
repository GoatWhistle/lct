"""Настройки. Нормативы ГОСТ — в миллисекундах и из конфига, не из кода."""

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.timers import NORMATIVES, TimerCode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Данные
    database_url: str = "postgresql+asyncpg://lct:lct@localhost:5432/lct"

    # Голосовой контур
    models_dir: str = "models"
    stt_model: str = "gigaam-v3-rnnt"
    endpointing_ms: int = 600
    mic_sample_rate: int = 16_000
    tts_sample_rate: int = 24_000
    offline: bool = False

    # LLM. Провайдер меняется значением, не кодом (docs/arch/STACK.md).
    # Имена COMPAT_MODEL_* принимаются тоже — так их выставляет командный сниппет.
    llm_provider: str = "openai_compatible"
    llm_base_url: str = Field(
        default="", validation_alias=AliasChoices("llm_base_url", "compat_model_url")
    )
    llm_api_key: str = Field(
        default="", validation_alias=AliasChoices("llm_api_key", "compat_model_api_key"),
        repr=False,
    )
    llm_model_caller: str = Field(
        default="", validation_alias=AliasChoices("llm_model_caller", "compat_model_name")
    )
    llm_model_judge: str = Field(
        default="", validation_alias=AliasChoices("llm_model_judge", "compat_model_name")
    )
    judge_temperature: float = 0.0

    # Нормативы: переопределяют значения по умолчанию из domain/timers.py
    timer_limits_ms: dict[TimerCode, int] = {}

    def limit_ms(self, code: TimerCode) -> int:
        return self.timer_limits_ms.get(code, NORMATIVES[code].limit_ms)


@lru_cache
def get_settings() -> Settings:
    return Settings()
