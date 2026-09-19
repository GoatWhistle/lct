"""make llm-check: один запрос к LLM, чтобы убедиться, что ключ и адрес рабочие.

Ключ берётся из backend/.env и никуда не печатается. Провайдер отвечает с машины
разработки напрямую; таймаут почти всегда означает не отказ провайдера, а окружение —
VPN-туннель или песочница, через которые российские адреса не проходят. Поэтому
проверять отсюда, а не из обёрток, и с той машины, на которой пойдёт занятие.
"""

import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    if not settings.llm_api_key:
        print("в backend/.env нет LLM_API_KEY")
        return 1

    print(f"адрес:  {settings.llm_base_url}")
    print(f"модель: {settings.llm_model_caller}")

    body = {
        "model": settings.llm_model_caller,
        "messages": [
            {"role": "system", "content": "Ты звонящий в службу 112, у тебя горит балкон. "
                                          "Ответь одной короткой фразой, в панике."},
            {"role": "user", "content": "Служба 112, что у вас случилось?"},
        ],
        "max_tokens": 60,
        "temperature": 0.8,
    }

    started = time.monotonic()
    try:
        response = httpx.post(
            f"{settings.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json=body,
            timeout=60,
        )
    except httpx.HTTPError as exc:
        print(f"\nсеть: {type(exc).__name__} — до провайдера не достучались.")
        print("Это чаще про окружение, чем про провайдера: проверь, что российские")
        print("адреса идут мимо VPN, и повтори с самой машины стенда.")
        return 2

    elapsed = time.monotonic() - started
    print(f"\nHTTP {response.status_code}, {elapsed:.2f} с")

    if response.status_code == 200:
        data = response.json()
        print("ответ модели:", data["choices"][0]["message"]["content"].strip())
        usage = data.get("usage", {})
        print("токены:", usage.get("prompt_tokens"), "→", usage.get("completion_tokens"))
        print("\nКЛЮЧ РАБОТАЕТ")
        return 0

    print("ответ сервера:", response.text[:400])
    if response.status_code in (401, 403):
        print("\nСеть в порядке, но ключ не принят: возможно, его нужно менять "
              "на временный токен — сверься с консолью Cloud.ru.")
    elif response.status_code == 404:
        print("\nКлюч принят, но такой модели нет: проверь LLM_MODEL_CALLER.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
