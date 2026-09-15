"""Настройки. Нормативы ГОСТ — в миллисекундах и из конфига, не из кода."""

from functools import lru_cache

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
    llm_provider: str = "gigachat"
    llm_api_key: str = ""
    llm_model_caller: str = ""
    llm_model_judge: str = ""
    judge_temperature: float = 0.0

    # Нормативы: переопределяют значения по умолчанию из domain/timers.py
    timer_limits_ms: dict[TimerCode, int] = {}

    def limit_ms(self, code: TimerCode) -> int:
        return self.timer_limits_ms.get(code, NORMATIVES[code].limit_ms)


@lru_cache
def get_settings() -> Settings:
    return Settings()
