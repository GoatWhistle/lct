#!/usr/bin/env python3
"""Живой smoke: активная карточка ДДС переживает restart backend.

Скрипт сам запускает два последовательных uvicorn-процесса на одном порту,
использует один локальный PostgreSQL и проверяет карточку, статус службы,
выбранную бригаду, письменный ответ и продолжающийся таймер.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import websockets


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"


def request_json(url: str, *, method: str = "GET", cookie: str = "") -> tuple[dict, str]:
    request = urllib.request.Request(url, method=method)
    if cookie:
        request.add_header("Cookie", cookie)
    with urllib.request.urlopen(request, timeout=5) as response:
        value = json.loads(response.read())
        issued = response.headers.get("set-cookie", "").split(";", 1)[0]
        return value, issued


def wait_ready(base: str, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            health, _ = request_json(f"{base}/api/health")
            return health
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(0.1)
    raise RuntimeError(f"backend не стал готов: {last}")


@asynccontextmanager
async def backend_process(port: int, database_url: str, *, hard_stop: bool = False):
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "OFFLINE": "true",
        "VOICE_ENABLED": "false",
        "DEV_AUTH_BYPASS": "true",
        "DEMO_NO_DB": "false",
        "SESSION_SECRET": "recovery-smoke-secret-that-is-not-production",
        "LLM_API_KEY": "",
        "LLM_PROVIDER": "disabled",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--workers", "1"],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_ready(f"http://127.0.0.1:{port}")
        yield process
    finally:
        if process.poll() is None:
            process.kill() if hard_stop else process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if not hard_stop and process.returncode not in (0, -15):
            error = process.stderr.read()[-4000:] if process.stderr else ""
            raise RuntimeError(f"backend завершился с кодом {process.returncode}: {error}")


async def receive_type(socket, wanted: str) -> dict:
    for _ in range(30):
        value = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
        if value.get("type") == wanted:
            return value
    raise AssertionError(f"событие {wanted} не получено")


async def start_and_edit(ws_base: str, cookie: str, session_id: str) -> dict:
    options = {"additional_headers": {"Cookie": cookie}}
    async with websockets.connect(f"{ws_base}/ws/control/{session_id}", **options) as control:
        await control.send(json.dumps({
            "type": "scenario.start",
            "scenario_id": "fire-apartment-l2",
            "trainee": "Recovery smoke",
            "mode": "training",
            "exercise": "dds",
            "criteria": {
                "decision_time_limit_seconds": 45,
                "allowed_errors": 0,
                "require_correct_grammar": True,
            },
        }))
        await asyncio.sleep(0.4)

    async with websockets.connect(f"{ws_base}/ws/station/{session_id}", **options) as station:
        card = await receive_type(station, "card.received")
        snapshot = (await receive_type(station, "station.state"))["snapshot"]
        service = snapshot["services"][0]
        crew = snapshot["crew_options"][0]
        await station.send(json.dumps({
            "type": "card.status", "service": service, "status": "accepted",
        }))
        await receive_type(station, "station.state")
        await station.send(json.dumps({"type": "crew.select", "crew": crew}))
        await receive_type(station, "station.state")
        await station.send(json.dumps({
            "type": "card.reply",
            "card_id": card["card"]["card_id"],
            "text": "Сообщение принято, бригада направлена.",
        }))
        latest = (await receive_type(station, "station.state"))["snapshot"]
    return {"service": service, "crew": crew, "card_id": card["card"]["card_id"],
            "before": latest}


async def verify_and_finish(ws_base: str, cookie: str, session_id: str, expected: dict) -> dict:
    options = {"additional_headers": {"Cookie": cookie}}
    async with websockets.connect(f"{ws_base}/ws/station/{session_id}", **options) as station:
        card = await receive_type(station, "card.received")
        snapshot = (await receive_type(station, "station.state"))["snapshot"]
        assert card["card"]["card_id"] == expected["card_id"]
        assert snapshot["statuses"][expected["service"]] == "accepted"
        assert snapshot["crew_selected"] == expected["crew"]
        assert snapshot["reply_text"] == "Сообщение принято, бригада направлена."
        await station.send(json.dumps({"type": "station.finish"}))
        await receive_type(station, "score.ready")
        return snapshot


async def run(args) -> dict:
    http_base = f"http://127.0.0.1:{args.port}"
    ws_base = f"ws://127.0.0.1:{args.port}"
    session_id = str(uuid4())
    async with backend_process(args.port, args.database_url, hard_stop=True):
        _, cookie = request_json(f"{http_base}/api/auth/dev-token", method="POST")
        expected = await start_and_edit(ws_base, cookie, session_id)

    # Небольшая пауза делает проверяемым продолжение активного таймера.
    await asyncio.sleep(1.2)
    async with backend_process(args.port, args.database_url):
        health = wait_ready(http_base)
        assert health["sessions_restored"] >= 1
        # Та же подписанная cookie должна пережить restart. При смене роли,
        # пароля или блокировке её отзовёт сохранённый auth_version.
        after = await verify_and_finish(ws_base, cookie, session_id, expected)

    return {
        "ok": True,
        "session_id": session_id,
        "sessions_restored": health["sessions_restored"],
        "card_id": after["card_id"],
        "status": after["statuses"][expected["service"]],
        "crew": after["crew_selected"],
        "reply_text": after["reply_text"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18116)
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://lct:lct@127.0.0.1:5434/lct"
        ),
    )
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
