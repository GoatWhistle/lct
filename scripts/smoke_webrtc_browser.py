#!/usr/bin/env python3
"""Два Chromium-клиента: WSS-регистрация, WebRTC-вызов 6101↔6102 и запись."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from load_browser import local_url, login_cookie
from smoke_sip import compose_password

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.auth import hash_password  # noqa: E402
from app.db.models import AuditLog, User  # noqa: E402


async def create_account(database_url: str, login: str, password: str) -> None:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add(User(
                login=login,
                full_name="WebRTC browser smoke",
                password_hash=hash_password(password),
                role="instructor",
                blocked=False,
            ))
            await db.commit()
    finally:
        await engine.dispose()


async def cleanup_account(database_url: str, login: str) -> None:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            await db.execute(delete(User).where(User.login == login))
            await db.execute(delete(AuditLog).where(AuditLog.actor == login))
            await db.commit()
    finally:
        await engine.dispose()


def recordings() -> dict[str, int]:
    result = subprocess.run(
        [
            "docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.sip.yml",
            "exec", "-T", "sip", "find", "/recordings", "-maxdepth", "1", "-type", "f",
            "-name", "*.wav", "-printf", "%f %s\\n",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    found: dict[str, int] = {}
    for line in result.stdout.splitlines():
        name, _, size = line.rpartition(" ")
        if name and size.isdigit():
            found[name] = int(size)
    return found


async def configure(page, extension: str, password: str, target: str, timeout_ms: float) -> None:
    await page.get_by_label("SIP внутренний номер").fill(extension)
    await page.get_by_label("Пароль SIP").fill(password)
    await page.get_by_role("button", name="Подключить телефон").click()
    await page.get_by_text("зарегистрирован в Asterisk", exact=False).wait_for(timeout=timeout_ms)
    await page.get_by_label("Номер SIP собеседника").fill(target)


async def run(args: argparse.Namespace) -> int:
    frontend = local_url(args.frontend_url, {"https"})
    backend = local_url(args.backend_url, {"http"})
    login = f"webrtc-smoke-{secrets.token_hex(5)}"
    app_password = secrets.token_urlsafe(24)
    await create_account(args.database_url, login, app_password)
    try:
        cookie = login_cookie(backend, login, app_password)
        cookie_name, cookie_value = cookie.split("=", 1)
        sip_6101 = compose_password("6101")
        sip_6102 = compose_password("6102")
        before = recordings()
        errors: list[str] = []
        started = time.perf_counter()
        timeout_ms = args.timeout * 1000

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
            ])
            contexts = []
            pages = []
            for _ in range(2):
                context = await browser.new_context(ignore_https_errors=True)
                await context.grant_permissions(["microphone"], origin=frontend)
                await context.add_cookies([{
                    "name": cookie_name,
                    "value": cookie_value,
                    "url": frontend,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }])
                page = await context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(f"{frontend}/phone", wait_until="networkidle", timeout=timeout_ms)
                contexts.append(context)
                pages.append(page)

            await asyncio.gather(
                configure(pages[0], "6101", sip_6101, "6102", timeout_ms),
                configure(pages[1], "6102", sip_6102, "6101", timeout_ms),
            )
            registered_ms = (time.perf_counter() - started) * 1000
            await pages[0].get_by_role("button", name="Позвонить").click()
            await pages[1].get_by_role("button", name="Ответить · 6101").wait_for(timeout=timeout_ms)
            await pages[1].get_by_role("button", name="Ответить · 6101").click()
            await asyncio.gather(
                pages[0].get_by_text("разговор идёт", exact=False).wait_for(timeout=timeout_ms),
                pages[1].get_by_text("разговор идёт", exact=False).wait_for(timeout=timeout_ms),
            )
            await asyncio.gather(
                pages[0].wait_for_function(
                    "document.querySelector('audio')?.srcObject?.getAudioTracks().length > 0",
                    timeout=timeout_ms,
                ),
                pages[1].wait_for_function(
                    "document.querySelector('audio')?.srcObject?.getAudioTracks().length > 0",
                    timeout=timeout_ms,
                ),
            )
            connected_ms = (time.perf_counter() - started) * 1000
            await asyncio.sleep(1)
            await pages[0].get_by_role("button", name="Завершить").click()
            await asyncio.gather(
                pages[0].get_by_text("зарегистрирован в Asterisk", exact=False).wait_for(timeout=timeout_ms),
                pages[1].get_by_text("зарегистрирован в Asterisk", exact=False).wait_for(timeout=timeout_ms),
            )
            for context in contexts:
                await context.close()
            await browser.close()

        deadline = time.monotonic() + 10
        after = recordings()
        new_recordings = {name: size for name, size in after.items() if name not in before and size > 44}
        while not new_recordings and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            after = recordings()
            new_recordings = {name: size for name, size in after.items() if name not in before and size > 44}

        passed = not errors and bool(new_recordings)
        result = {
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "frontend": frontend,
            "extensions": ["6101", "6102"],
            "registered_clients": 2,
            "registered_ms": round(registered_ms, 3),
            "call_connected_ms": round(connected_ms, 3),
            "remote_audio_tracks": [1, 1],
            "browser_errors": errors,
            "new_recordings": new_recordings,
            "passed": passed,
            "scope": "two isolated Chromium contexts; WSS/SIP Digest; WebRTC audio tracks; Asterisk MixMonitor",
        }
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        print(rendered)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        return 0 if passed else 1
    finally:
        await cleanup_account(args.database_url, login)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-url", default="https://127.0.0.1:5443")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--database-url", default="postgresql+asyncpg://lct:lct@127.0.0.1:5432/lct"
    )
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
