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
from sqlalchemy import select
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
    *,
    remove_accounts: bool,
) -> None:
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            await db.execute(delete(Session).where(Session.id == session_id))
            await db.execute(delete(AuditLog).where(AuditLog.object_id == str(session_id)))
            if remove_accounts:
                await db.execute(delete(AuditLog).where(AuditLog.actor.in_(logins)))
                await db.execute(delete(User).where(User.login.in_(logins)))
                await db.execute(delete(Trainee).where(Trainee.name == trainee_name))
            await db.commit()
    finally:
        await engine.dispose()


async def compose_service(project: str, action: str, service: str) -> None:
    process = await asyncio.create_subprocess_exec(
        "docker", "compose", "--project-name", project, action, service,
        cwd=ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode:
        detail = (stderr or stdout).decode(errors="replace").strip()
        raise RuntimeError(f"docker compose {action} {service}: {detail}")


async def run(args: argparse.Namespace) -> int:
    frontend = local_url(args.frontend_url, {"https"})
    backend = local_url(args.backend_url, {"http"})
    if not args.compose_project.strip():
        raise ValueError("--compose-project обязателен: recovery test останавливает Nginx")
    if not 0 <= args.outage_seconds <= 30:
        raise ValueError("--outage-seconds должен быть от 0 до 30")
    ephemeral = not args.login and not args.password
    if bool(args.login) != bool(args.password):
        raise ValueError("login и password задаются вместе либо не задаются")
    instructor_login = args.login
    instructor_password = args.password
    trainee_login = args.trainee_login
    trainee_password = args.trainee_password
    trainee_name = ""
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
    else:
        if not trainee_login or not trainee_password:
            raise ValueError("для существующих аккаунтов нужны trainee-login и trainee-password")
        engine = create_async_engine(args.database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                row = (await db.execute(
                    select(Trainee.id, Trainee.name)
                    .join(User, User.trainee_id == Trainee.id)
                    .where(
                        User.login == trainee_login,
                        User.role == "trainee",
                        User.blocked.is_(False),
                    )
                )).one_or_none()
                if row is None:
                    raise ValueError("учётка курсанта не найдена или не привязана к профилю")
                trainee_id, trainee_name = row
        finally:
            await engine.dispose()
    assert instructor_login and instructor_password and trainee_login and trainee_password
    instructor_cookie = login_cookie(backend, instructor_login, instructor_password)
    trainee_cookie = login_cookie(backend, trainee_login, trainee_password)
    cookie_name, cookie_value = trainee_cookie.split("=", 1)
    ws_base = "ws" + backend.removeprefix("http")
    session_id = str(session_uuid)
    started = False
    frontend_stopped = False
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
            await page.add_init_script("""(() => {
              window.__lctDropOperatorKioAck = false;
              window.__lctOperatorAckDropped = false;
              let owner = WebSocket.prototype;
              let descriptor;
              while (owner && !descriptor) {
                descriptor = Object.getOwnPropertyDescriptor(owner, 'onmessage');
                if (!descriptor) owner = Object.getPrototypeOf(owner);
              }
              if (!owner || !descriptor?.set || !descriptor?.get) return;
              Object.defineProperty(owner, 'onmessage', {
                configurable: descriptor.configurable,
                enumerable: descriptor.enumerable,
                get() { return descriptor.get.call(this); },
                set(handler) {
                  const socket = this;
                  const wrapped = (event) => {
                    if (window.__lctDropOperatorKioAck && typeof event.data === 'string') {
                      try {
                        const message = JSON.parse(event.data);
                        if (message.type === 'kio.patch' && message.source === 'operator') {
                          window.__lctDropOperatorKioAck = false;
                          window.__lctOperatorAckDropped = true;
                          socket.close(4000, 'test: discard checkpoint acknowledgement');
                          return;
                        }
                      } catch { /* non-JSON frames are passed to the application */ }
                    }
                    if (handler) handler.call(socket, event);
                  };
                  descriptor.set.call(socket, handler ? wrapped : null);
                },
              });
              window.__lctAckInterceptorInstalled = Boolean(descriptor?.set && descriptor?.get);
            })()""")
            await page.goto(
                f"{frontend}/trainee?session={session_id}",
                wait_until="domcontentloaded",
                timeout=args.timeout * 1000,
            )
            address = page.locator(".kio-arm-row-address input")
            await address.wait_for(
                timeout=args.timeout * 1000,
            )
            if not await page.evaluate("window.__lctAckInterceptorInstalled"):
                raise RuntimeError("browser could not install the WebSocket acknowledgement test hook")
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
            # Останавливаем proxy и вводим значение только после того, как
            # browser отметил канал отключённым: это проверяет offline outbox.
            await compose_service(args.compose_project, "stop", "frontend")
            frontend_stopped = True
            outage_started_at = time.perf_counter()
            await page.wait_for_function(
                "window.__lctRecoveryStates.some(value => !value.includes('АРМ подключён'))",
                timeout=args.timeout * 1000,
            )
            # The operator edits only after the channel is known to be down.
            # This exercises the disconnected outbox path instead of relying
            # on a frame which might already have reached the server.
            await address.fill(buffered_value)
            await page.locator(".kio-arm-row-address.kio-arm-pending").wait_for(
                timeout=args.timeout * 1000,
            )
            outage_remaining = args.outage_seconds - (time.perf_counter() - outage_started_at)
            if outage_remaining > 0:
                await asyncio.sleep(outage_remaining)
            restore_started_at = time.perf_counter()
            proxy_unavailable_seconds = restore_started_at - outage_started_at
            await compose_service(args.compose_project, "start", "frontend")
            frontend_stopped = False
            await page.wait_for_function(
                "window.__lctRecoveryStates.at(-1).includes('АРМ подключён')",
                timeout=args.timeout * 1000,
            )
            recovery_seconds = time.perf_counter() - restore_started_at
            await page.wait_for_function(
                "!document.querySelector('.kio-arm-row-address')?.classList.contains('kio-arm-pending')",
                timeout=args.timeout * 1000,
            )
            buffered_value_after = await page.locator(".kio-arm-row-address input").input_value()

            # Now send while the WSS is open, allow PostgreSQL checkpoint/echo
            # to complete, then deliberately discard that one echo and close
            # the browser socket. This targets the ambiguous commit/ack window:
            # reconnect must recover the stored value without duplicating a
            # non-replace-style action or leaving the field pending.
            committed_value = "улица Подтверждённая, дом 31"
            await page.evaluate("window.__lctDropOperatorKioAck = true")
            await address.fill(committed_value)
            await page.wait_for_function(
                "window.__lctOperatorAckDropped === true",
                timeout=args.timeout * 1000,
            )
            await page.wait_for_function(
                "window.__lctRecoveryStates.some(value => !value.includes('АРМ подключён'))",
                timeout=args.timeout * 1000,
            )
            await page.wait_for_function(
                "window.__lctRecoveryStates.at(-1).includes('АРМ подключён')",
                timeout=args.timeout * 1000,
            )
            await page.wait_for_function(
                "!document.querySelector('.kio-arm-row-address')?.classList.contains('kio-arm-pending')",
                timeout=args.timeout * 1000,
            )
            committed_value_after = await page.locator(".kio-arm-row-address input").input_value()
            ack_deliberately_dropped = await page.evaluate("window.__lctOperatorAckDropped")
            pending_cleared = not await page.locator(".kio-arm-row-address").evaluate(
                "element => element.classList.contains('kio-arm-pending')"
            )
            total_seconds = time.perf_counter() - started_at
            states = await page.evaluate("window.__lctRecoveryStates")
            await context.close()
            await browser.close()

        result = {
            "frontend": frontend,
            "session_id": session_id,
            "component_restarted": "frontend (Nginx TLS/WSS proxy) container stop/start",
            "observed_states": states,
            "requested_outage_seconds": args.outage_seconds,
            "proxy_unavailable_seconds": proxy_unavailable_seconds,
            "recovery_after_restore_seconds": recovery_seconds,
            "total_seconds": total_seconds,
            "browser_errors": errors,
            "buffered_patch": {
                "field": "address",
                "value": buffered_value_after,
                "confirmed_by_server": buffered_value_after == buffered_value,
            },
            "lost_ack_recovery": {
                "ack_deliberately_dropped": ack_deliberately_dropped,
                "value_after_reconnect": committed_value_after,
                "confirmed_by_recovered_server_state": committed_value_after == committed_value,
                "pending_cleared": pending_cleared,
            },
            "pass_recovery_30s": (
                args.outage_seconds <= 30
                and proxy_unavailable_seconds <= 30.5
                and recovery_seconds <= 30
                and not errors
                and buffered_value_after == buffered_value
                and committed_value_after == committed_value
                and ack_deliberately_dropped
                and pending_cleared
            ),
            "scope": (
                "one live card exercise; Nginx is unavailable while the trainee enters an address; "
                "the disconnected KIO patch is queued and confirmed after WSS reconnect; a second "
                "KIO patch is committed while connected, its checkpoint echo is deliberately "
                "discarded, and reconnect recovers that state; backend remains running"
            ),
        }
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        print(rendered)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        return 0 if result["pass_recovery_30s"] else 1
    finally:
        if frontend_stopped:
            await compose_service(args.compose_project, "start", "frontend")
        if started:
            await control(ws_base, session_id, instructor_cookie, {"type": "session.stop"})
        if ephemeral:
            await cleanup_temp_accounts(
                args.database_url,
                [instructor_login, trainee_login],
                trainee_name,
                session_uuid,
                remove_accounts=ephemeral,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-url", default="https://127.0.0.1:5443")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--compose-project", required=True,
                        help="точное имя изолированного Compose project; тест останавливает его frontend")
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
    parser.add_argument("--outage-seconds", type=float, default=0,
                        help="держать frontend недоступным столько секунд (0–30), вводить карточку после разрыва")
    parser.add_argument("--ignore-https-errors", action="store_true")
    parser.add_argument("--output")
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except Exception as exc:
        print(f"recovery check failed: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
