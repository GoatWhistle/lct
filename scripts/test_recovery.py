#!/usr/bin/env python3
"""Замер переподключения браузерного WSS после перезапуска frontend-proxy."""

import argparse
import asyncio
import json
import secrets
import sys
import time
from pathlib import Path
from uuid import uuid4

from playwright.async_api import async_playwright
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from load_browser import control, local_url, login_cookie

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.auth import hash_password  # noqa: E402
from app.db.models import AuditLog, Session, Trainee, User  # noqa: E402


async def create_temp_accounts(
    database_url: str,
    instructor_login: str,
    instructor_password: str,
    trainee_login: str,
    trainee_password: str,
    trainee_name: str,
):
    """Создать изолированную пару ролей и вернуть ID привязанного курсанта."""
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            trainee = Trainee(name=trainee_name)
            db.add(trainee)
            await db.flush()
            db.add_all([
                User(
                    login=instructor_login,
                    full_name="Преподаватель проверки восстановления",
                    password_hash=hash_password(instructor_password),
                    role="instructor",
                    blocked=False,
                ),
                User(
                    login=trainee_login,
                    full_name=trainee_name,
                    password_hash=hash_password(trainee_password),
                    role="trainee",
                    trainee_id=trainee.id,
                    blocked=False,
                ),
            ])
            await db.commit()
            return trainee.id
    finally:
        await engine.dispose()


async def cleanup_temp_accounts(
    database_url: str,
    logins: list[str],
    trainee_name: str,
    session_id,
) -> None:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            await db.execute(delete(Session).where(Session.id == session_id))
            await db.execute(delete(User).where(User.login.in_(logins)))
            await db.execute(delete(Trainee).where(Trainee.name == trainee_name))
            await db.execute(delete(AuditLog).where(AuditLog.actor.in_(logins)))
            await db.commit()
    finally:
        await engine.dispose()


async def compose_service(action: str, service: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "docker", "compose", action, service,
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        detail = (stderr or stdout).decode(errors="replace").strip()
        raise RuntimeError(f"docker compose {action} {service}: {detail}")


async def wait_service_healthy(service: str, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process = await asyncio.create_subprocess_exec(
            "docker", "compose", "ps", service,
            cwd=ROOT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await process.communicate()
        if process.returncode == 0 and b"(healthy)" in stdout:
            return
        await asyncio.sleep(0.5)
    raise TimeoutError(f"контейнер {service} не стал healthy за {timeout} с")


async def run(args: argparse.Namespace) -> int:
    frontend = local_url(args.frontend_url, {"https"})
    backend = local_url(args.backend_url, {"http"})
    ephemeral = not args.login and not args.password
    if bool(args.login) != bool(args.password):
        raise ValueError("login и password задаются вместе либо не задаются")
    instructor_login = args.login
    instructor_password = args.password
    trainee_login = args.trainee_login
    trainee_password = args.trainee_password
    trainee_name = "recovery-check"
    trainee_id = None
    session_uuid = uuid4()
    if ephemeral:
        suffix = session_uuid.hex[:10]
        instructor_login = f"recovery-instructor-{suffix}"
        instructor_password = secrets.token_urlsafe(24)
        trainee_login = f"recovery-trainee-{suffix}"
        trainee_password = secrets.token_urlsafe(24)
        trainee_name = f"recovery-trainee-{suffix}"
        trainee_id = await create_temp_accounts(
            args.database_url,
            instructor_login,
            instructor_password,
            trainee_login,
            trainee_password,
            trainee_name,
        )
    elif not trainee_login or not trainee_password:
        raise ValueError("для существующих аккаунтов нужны trainee-login и trainee-password")
    assert instructor_login and instructor_password and trainee_login and trainee_password
    instructor_cookie = login_cookie(backend, instructor_login, instructor_password)
    trainee_cookie = login_cookie(backend, trainee_login, trainee_password)
    cookie_name, cookie_value = trainee_cookie.split("=", 1)
    ws_base = "ws" + backend.removeprefix("http")
    session_id = str(session_uuid)
    started = False
    frontend_stopped = False
    backend_paused = False
    try:
        await control(ws_base, session_id, instructor_cookie, {
            "type": "scenario.start",
            "scenario_id": args.scenario,
            "trainee": trainee_name,
            "trainee_id": str(trainee_id) if trainee_id else None,
            "mode": "training",
            "exercise": "card",
        })
        started = True
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 720},
                ignore_https_errors=args.ignore_https_errors,
            )
            await context.add_cookies([{
                "name": cookie_name,
                "value": cookie_value,
                "url": frontend,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }])
            page = await context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.goto(
                f"{frontend}/trainee?session={session_id}",
                wait_until="domcontentloaded",
                timeout=args.timeout * 1000,
            )
            address = page.locator(".kio-arm-row-address input")
            await address.wait_for(
                timeout=args.timeout * 1000,
            )
            await page.locator(".arm-topbar-state .state-ok").wait_for(
                timeout=args.timeout * 1000,
            )
            await page.evaluate("""() => {
              const node = document.querySelector('.arm-topbar-state');
              window.__lctRecoveryStates = [node?.textContent ?? 'missing'];
              new MutationObserver(() => window.__lctRecoveryStates.push(
                node?.textContent ?? 'missing'
              )).observe(node, {attributes: true, childList: true, subtree: true});
            }""")

            buffered_value = "улица Буферная, дом 30"
            started_at = time.perf_counter()
            # Держим существующее WSS-соединение открытым, но не даём backend
            # подтвердить правку. Это воспроизводит пакет, зависший ровно в
            # момент отказа прокси, без искусственного доступа к JS-сокету.
            await compose_service("pause", "backend")
            backend_paused = True
            await address.fill(buffered_value)
            await page.locator(".kio-arm-row-address.kio-arm-pending").wait_for(
                timeout=args.timeout * 1000,
            )
            await compose_service("stop", "frontend")
            frontend_stopped = True
            await page.wait_for_function(
                "window.__lctRecoveryStates.some(value => !value.includes('АРМ подключён'))",
                timeout=args.timeout * 1000,
            )
            await compose_service("unpause", "backend")
            backend_paused = False
            await wait_service_healthy("backend")
            await compose_service("start", "frontend")
            frontend_stopped = False
            await page.wait_for_function(
                "window.__lctRecoveryStates.at(-1).includes('АРМ подключён')",
                timeout=args.timeout * 1000,
            )
            await page.wait_for_function(
                "!document.querySelector('.kio-arm-row-address')?.classList.contains('kio-arm-pending')",
                timeout=args.timeout * 1000,
            )
            buffered_value_after = await page.locator(".kio-arm-row-address input").input_value()
            recovery_seconds = time.perf_counter() - started_at
            states = await page.evaluate("window.__lctRecoveryStates")
            await context.close()
            await browser.close()

        result = {
            "frontend": frontend,
            "session_id": session_id,
            "component_restarted": "frontend (Nginx TLS/WSS proxy) container stop/start",
            "observed_states": states,
            "recovery_seconds": recovery_seconds,
            "browser_errors": errors,
            "buffered_patch": {
                "field": "address",
                "value": buffered_value_after,
                "confirmed_by_server": buffered_value_after == buffered_value,
            },
            "pass_recovery_30s": (
                recovery_seconds <= 30 and not errors and buffered_value_after == buffered_value
            ),
            "scope": (
                "one live card exercise; backend acknowledgement is stalled after address edit; "
                "Nginx is stopped; after recovery reconnect repeats only the idempotent KIO patch"
            ),
        }
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        print(rendered)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        return 0 if result["pass_recovery_30s"] else 1
    finally:
        if backend_paused:
            await compose_service("unpause", "backend")
            await wait_service_healthy("backend")
        if frontend_stopped:
            await compose_service("start", "frontend")
        if started:
            await control(ws_base, session_id, instructor_cookie, {"type": "session.stop"})
        if ephemeral:
            await cleanup_temp_accounts(
                args.database_url,
                [instructor_login, trainee_login],
                trainee_name,
                session_uuid,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-url", default="https://127.0.0.1:5443")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--login")
    parser.add_argument("--password")
    parser.add_argument("--trainee-login")
    parser.add_argument("--trainee-password")
    parser.add_argument(
        "--database-url",
        default="postgresql+asyncpg://lct:lct@127.0.0.1:5432/lct",
    )
    parser.add_argument("--scenario", default="fire-apartment-l2")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--ignore-https-errors", action="store_true")
    parser.add_argument("--output")
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except Exception as exc:
        print(f"recovery check failed: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
