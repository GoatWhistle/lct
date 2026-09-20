"""make lesson: запустить занятие и напечатать ссылки на экраны.

Нужен, пока нет пульта преподавателя (lct-17): без запущенного занятия
АРМ курсанта открыть не с чем. Занятие живёт в памяти сервера и после
выхода скрипта остаётся запущенным.

    make lesson                                  сценарий по умолчанию, тренировочный режим
    make lesson s=fire-apartment-l2 m=exam
"""

import asyncio
import json
import sys
import urllib.request
import uuid

import websockets

BACKEND = "ws://localhost:8000"
FRONTEND = "http://localhost:5173"


def dev_cookie() -> str:
    """Вход для служебного скрипта.

    Сокеты закрыты ролями (lct-23), а `make lesson` — наш инструмент, а не
    пользователь. Точка `dev-token` живёт за флагом `DEV_AUTH_BYPASS` и на
    рабочем стенде отвечает 404 — тогда скрипт честно скажет, что занятие
    запускается с пульта преподавателя.
    """
    request = urllib.request.Request(
        f"{BACKEND.replace('ws://', 'http://')}/api/auth/dev-token", method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.headers.get("set-cookie", "").split(";")[0]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SystemExit(
                "вход выключен: на стенде занятие запускается с пульта преподавателя.\n"
                "Для разработки поставь DEV_AUTH_BYPASS=true в backend/.env"
            ) from exc
        raise


async def main(scenario_id: str, mode: str) -> None:
    session_id = str(uuid.uuid4())
    headers = {"Cookie": dev_cookie()}
    async with websockets.connect(
        f"{BACKEND}/ws/control/{session_id}", additional_headers=headers
    ) as control:
        await control.send(json.dumps({
            "type": "scenario.start",
            "scenario_id": scenario_id,
            "trainee": "Курсант",
            "mode": mode,
        }))
        await asyncio.sleep(0.5)  # дать серверу зарегистрировать занятие до закрытия сокета

    print(f"занятие {session_id} — {scenario_id}, режим {mode}")
    print(f"  курсант:       {FRONTEND}/trainee?session={session_id}")
    print(f"  монитор:       {FRONTEND}/wall?session={session_id}")
    print(f"  преподаватель: {FRONTEND}/instructor?session={session_id}")


if __name__ == "__main__":
    scenario = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "fire-apartment-l2"
    mode = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else "training"
    asyncio.run(main(scenario, mode))
