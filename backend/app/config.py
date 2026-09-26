"""Настройки. Нормативы ГОСТ — в миллисекундах и из конфига, не из кода."""

import platform
from functools import lru_cache
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.timers import NORMATIVES, TimerCode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # `production` activates fail-closed checks for session signing and cookies.
    app_env: Literal["development", "production"] = "development"

    # Данные
    database_url: str = "postgresql+asyncpg://lct:lct@localhost:5432/lct"
    # HTTP auth and WebSocket handshakes both validate against PostgreSQL.
    # Keep enough warm slots for concurrent classroom joins without tying up
    # the whole server's PostgreSQL connection budget.
    db_pool_size: int = Field(default=20, ge=1, le=100)
    db_pool_max_overflow: int = Field(default=10, ge=0, le=100)
    # Unique and stable per backend process/container. Cluster deployments
    # should set this explicitly so a restart retains its session ownership.
    backend_node_id: str = Field(
        default_factory=lambda: platform.node() or "local",
        min_length=1,
        max_length=128,
    )
    # Только локальная демонстрация: живые занятия и отчёты в памяти, без
    # Postgres и без долговременного журнала. Не включать на учебном стенде.
    demo_no_db: bool = False

    # Голосовой контур
    models_dir: str = "models"
    stt_model: str = "whisper-small"
    # Локальный файл GGML Whisper small для whisper.cpp. Имя модели из сети
    # не принимается: учебный контур не скачивает веса при запуске.
    whisper_model_path: str = "models/whisper-small"
    whisper_server_url: str = "http://127.0.0.1:18082"
    endpointing_ms: int = 600
    mic_sample_rate: int = 16_000
    tts_sample_rate: int = 24_000
    offline: bool = False
    # STT/голос не входят в MVP ДДС; включаются лишь для отдельного эксперимента.
    voice_enabled: bool = False
    # Запись хранится локально и отдаётся только через авторизованный API.
    record_calls: bool = True
    recordings_dir: str = "recordings"
    # Автокопирование полного стенда. Интервал по умолчанию ровно сутки;
    # небольшой grace нужен только для отображения просрочки в мониторинге.
    backup_interval_seconds: int = 86_400
    backup_retry_seconds: int = 300
    backup_keep: int = 14

    # LLM. Провайдер меняется значением, не кодом (docs/arch/STACK.md).
    # Имена COMPAT_MODEL_* принимаются тоже — так их выставляет командный сниппет.
    llm_provider: str = "local"
    llm_base_url: str = Field(
        default="http://127.0.0.1:18080/v1",
        validation_alias=AliasChoices("llm_base_url", "compat_model_url"),
    )
    llm_api_key: str = Field(
        default="", validation_alias=AliasChoices("llm_api_key", "compat_model_api_key"),
        repr=False,
    )
    llm_model_caller: str = Field(
        default="Qwen3-1.7B", validation_alias=AliasChoices("llm_model_caller", "compat_model_name")
    )
    llm_model_judge: str = Field(
        default="", validation_alias=AliasChoices("llm_model_judge", "compat_model_name")
    )
    judge_temperature: float = 0.0
    # Vikhr — отдельный русскоязычный контрольный профиль диалога. Никакая
    # модель не решает, какие факты раскрыть, как маршрутизировать карточку или
    # какой балл поставить: это делает протокол и оценка в коде.
    llm_control_base_url: str = "http://127.0.0.1:18081/v1"
    llm_model_control: str = "Vikhr-1B"
    grammar_llm_enabled: bool = False
    assessment_feedback_enabled: bool = True
    dialogue_model_mode: str = "dialogue"  # dialogue | russian_control
    # Docker Desktop даёт контейнеру специальное имя хоста. Оно разрешается
    # только явным флагом: обычный OFFLINE по-прежнему принимает лишь literal
    # loopback и не может незаметно уйти во внешний API.
    allow_docker_host_models: bool = False

    # Вход и роли (lct-23). `session_secret` обязателен на рабочем стенде:
    # значение по умолчанию годится только для разработки, и администратор
    # видит в состоянии сервисов, что оно не заменено.
    session_secret: str = "dev-secret-поменять-на-стенде"
    # В TLS-профиле cookie уходит только по HTTPS/WSS. Для нативной разработки
    # на loopback флаг выключен, иначе браузер не сможет войти по HTTP.
    secure_cookies: bool = False
    #: Вход без пароля для `make lesson` и тестов. На стенде выключен.
    dev_auth_bypass: bool = False

    # Необязательная интеграция с локальным AD/LDAP-каталогом. Пароль
    # сервисной учётки никогда не показывается в диагностиках/config repr.
    # LDAP без TLS намеренно не поддерживается: используйте ldaps:// либо
    # ldap:// с обязательным StartTLS.
    ldap_enabled: bool = False
    ldap_url: str = ""
    ldap_base_dn: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = Field(default="", repr=False)
    ldap_user_filter: str = "(objectClass=person)"
    ldap_login_attribute: str = "sAMAccountName"
    ldap_role_groups: dict[str, str] = {}
    ldap_service_groups: dict[str, str] = {}
    ldap_ca_certs_file: str = ""
    ldap_connect_timeout_seconds: int = Field(default=5, ge=1, le=30)

    # Нормативы: переопределяют значения по умолчанию из domain/timers.py
    timer_limits_ms: dict[TimerCode, int] = {}

    def limit_ms(self, code: TimerCode) -> int:
        return self.timer_limits_ms.get(code, NORMATIVES[code].limit_ms)

    def validate_deployment_security(self) -> None:
        """Reject known development authentication defaults on a production app."""
        if self.app_env != "production":
            return
        problems: list[str] = []
        if self.demo_no_db:
            problems.append("DEMO_NO_DB must be disabled")
        if self.dev_auth_bypass:
            problems.append("DEV_AUTH_BYPASS must be disabled")
        if not self.offline:
            problems.append("OFFLINE must be enabled for the local training deployment")
        if self.llm_provider != "local":
            problems.append("LLM_PROVIDER must be local for the local training deployment")
        if self.session_secret == "dev-secret-поменять-на-стенде" or len(self.session_secret) < 32:
            problems.append("SESSION_SECRET must be a unique value of at least 32 characters")
        database_password = unquote(urlsplit(self.database_url).password or "")
        if (len(database_password) < 32
                or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._~-"
                       for char in database_password)):
            problems.append("PostgreSQL password must contain at least 32 URL-safe characters")
        if not self.secure_cookies:
            problems.append("SECURE_COOKIES must be enabled (HTTPS/WSS required)")
        if problems:
            raise ValueError("unsafe production security configuration: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
