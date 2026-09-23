"""Проверка активного диалогового профиля через тот же клиент, что и занятие.

В локальном режиме ключ не нужен; OFFLINE=true запрещает запрос наружу.
"""

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.dialog.llm import LlmClient, LlmRequest, LlmUnavailable  # noqa: E402


async def check() -> int:
    settings = get_settings()
    control = settings.dialogue_model_mode == "russian_control"
    model = settings.llm_model_control if control else settings.llm_model_caller
    address = settings.llm_control_base_url if control else settings.llm_base_url
    print(f"адрес: {address}\nмодель: {model}")
    client = LlmClient(base_url=address, timeout=30)
    started = time.monotonic()
    try:
        reply = await client.complete(LlmRequest(
            model=model,
            messages=[{"role": "user", "content": "Ответь одним словом по-русски: работает /no_think"}],
            temperature=0,
            max_tokens=32,
        ), use_cache=False)
    except LlmUnavailable as exc:
        print(f"модель недоступна: {exc}")
        return 1
    finally:
        await client.aclose()
    print(f"ответ за {time.monotonic() - started:.2f} с: {reply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(check()))
