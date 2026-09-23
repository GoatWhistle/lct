#!/usr/bin/env python3
"""Проверка: подробный JSON/CSV/PDF переживает рестарт backend и укладывается в 30 с."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

import websockets
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.auth import hash_password  # noqa: E402
from app.db.models import AuditLog, Session, Trainee, User  # noqa: E402


def local_http(value: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("backend должен быть локальным HTTP URL")
    return value.rstrip("/")


def login_cookie(base: str, login: str, password: str) -> str:
    request = urllib.request.Request(
        f"{base}/api/auth/login",
        data=json.dumps({"login": login, "password": password}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        cookie = response.headers.get("Set-Cookie", "").split(";", 1)[0]
    if not cookie.startswith("lct_session="):
        raise RuntimeError("login не выдал cookie")
    return cookie


def get_timed(base: str, path: str, cookie: str) -> tuple[bytes, str, float]:
    request = urllib.request.Request(f"{base}{path}", headers={"Cookie": cookie})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=35) as response:
        body = response.read()
        content_type = response.headers.get_content_type()
    return body, content_type, (time.perf_counter() - started) * 1000


async def receive_type(socket, wanted: str, limit: int = 40) -> dict:
    for _ in range(limit):
        raw = await asyncio.wait_for(socket.recv(), timeout=10)
        if isinstance(raw, str):
            event = json.loads(raw)
            if event.get("type") == wanted:
                return event
    raise RuntimeError(f"не получено событие {wanted}")


async def create_temp_user(database_url: str, login: str, password: str) -> None:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add(User(
                login=login,
                full_name="Проверка архивного отчёта",
                password_hash=hash_password(password),
                role="instructor",
                blocked=False,
            ))
            await db.commit()
    finally:
        await engine.dispose()


async def cleanup(database_url: str, login: str, trainee_name: str, session_id) -> None:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            await db.execute(delete(Session).where(Session.id == session_id))
            await db.execute(delete(User).where(User.login == login))
            await db.execute(delete(Trainee).where(Trainee.name == trainee_name))
            await db.execute(delete(AuditLog).where(AuditLog.actor == login))
            await db.commit()
    finally:
        await engine.dispose()


async def complete_card(base: str, cookie: str, session_id: str, trainee_name: str) -> None:
    ws_base = "ws" + base.removeprefix("http")
    header = "additional_headers" if "additional_headers" in inspect.signature(
        websockets.connect
    ).parameters else "extra_headers"
    options = {header: {"Cookie": cookie}, "open_timeout": 10, "ping_interval": None}
    async with websockets.connect(f"{ws_base}/ws/control/{session_id}", **options) as control:
        await control.send(json.dumps({
            "type": "scenario.start",
            "scenario_id": "fire-apartment-l2",
            "trainee": trainee_name,
            "mode": "training",
            "exercise": "card",
        }, ensure_ascii=False))
        # Control-сокет только отправляет директивы; подтверждение старта
        # получают рабочие АРМ, поэтому ждём обработки, а не несуществующий ACK.
        await asyncio.sleep(0.2)

    async with websockets.connect(f"{ws_base}/ws/call/{session_id}", **options) as trainee:
        await receive_type(trainee, "card.briefing")
        await trainee.send(json.dumps({
            "type": "kio.patch",
            "fields": {
                "address": "улица Ленина, 14",
                "floor": "5",
                "incident_type": "fire",
                "victims_count": 2,
                "description": "горит балкон",
                "signs": ["жилой дом", "балкон", "открытое пламя"],
            },
        }, ensure_ascii=False))
        await receive_type(trainee, "kio.patch")
        await trainee.send(json.dumps({"type": "card.submit"}))
        await receive_type(trainee, "score.ready")


def restart_backend(base: str) -> float:
    started = time.perf_counter()
    subprocess.run(["docker", "compose", "restart", "backend"], cwd=ROOT, check=True,
                   capture_output=True, text=True, timeout=60)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=2) as response:
                if response.status == 200:
                    return (time.perf_counter() - started) * 1000
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.25)
    raise TimeoutError("backend не восстановился после restart")


async def run(args: argparse.Namespace) -> int:
    base = local_http(args.backend_url)
    session_id = uuid4()
    suffix = uuid4().hex[:10]
    login = f"report-smoke-{suffix}"
    trainee_name = f"report-smoke-trainee-{suffix}"
    password = secrets.token_urlsafe(24)
    await create_temp_user(args.database_url, login, password)
    try:
        cookie = login_cookie(base, login, password)
        await complete_card(base, cookie, str(session_id), trainee_name)
        live_json, _, live_ms = get_timed(base, f"/api/sessions/{session_id}/report", cookie)
        if json.loads(live_json)["session_id"] != str(session_id):
            raise RuntimeError("живой отчёт относится к другой сессии")

        restart_ms = await asyncio.to_thread(restart_backend, base)
        cookie = login_cookie(base, login, password)
        json_body, json_type, json_ms = get_timed(base, f"/api/sessions/{session_id}/report", cookie)
        csv_body, csv_type, csv_ms = get_timed(base, f"/api/sessions/{session_id}/report.csv", cookie)
        pdf_body, pdf_type, pdf_ms = get_timed(base, f"/api/sessions/{session_id}/report.pdf", cookie)
        decoded = json.loads(json_body)
        times = [json_ms, csv_ms, pdf_ms]
        passed = (
            decoded["session_id"] == str(session_id)
            and decoded["score_final"] == 100
            and csv_body.startswith(b"\xef\xbb\xbf")
            and pdf_body.startswith(b"%PDF-")
            and json_type == "application/json"
            and csv_type == "text/csv"
            and pdf_type == "application/pdf"
            and max(times) <= 30_000
        )
        result = {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "session_id": str(session_id),
            "backend_restart_ms": round(restart_ms, 3),
            "live_json_ms": round(live_ms, 3),
            "persisted_json_ms": round(json_ms, 3),
            "persisted_csv_ms": round(csv_ms, 3),
            "persisted_pdf_ms": round(pdf_ms, 3),
            "pdf_bytes": len(pdf_body),
            "requirement_ms": 30_000,
            "passed": passed,
            "scope": "card exercise; real PostgreSQL; report fetched after backend restart",
        }
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        print(rendered)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        return 0 if passed else 1
    finally:
        await cleanup(args.database_url, login, trainee_name, session_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--database-url",
        default="postgresql+asyncpg://lct:lct@127.0.0.1:5432/lct",
    )
    parser.add_argument("--output", type=Path)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
