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
import uuid

import websockets

BACKEND = "ws://localhost:8000"
FRONTEND = "http://localhost:5173"


async def main(scenario_id: str, mode: str) -> None:
    session_id = str(uuid.uuid4())
    async with websockets.connect(f"{BACKEND}/ws/control/{session_id}") as control:
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
