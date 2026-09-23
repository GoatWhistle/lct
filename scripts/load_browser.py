#!/usr/bin/env python3
"""Локальная браузерная проверка 20 занятий / 100 Chromium-контекстов."""

import argparse
import asyncio
import inspect
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from uuid import uuid4

import websockets
from playwright.async_api import async_playwright


def local_url(value: str, schemes: set[str]) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in schemes or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"принимаются только локальные URL со схемами {sorted(schemes)}")
    return value.rstrip("/")


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


def timing_summary(values: list[float]) -> dict | None:
    if not values:
        return None
    return {
        "min": min(values),
        "median": statistics.median(values),
        "p95": percentile(values, 95),
        "max": max(values),
    }


def login_cookie(base: str, login: str, password: str) -> str:
    body = json.dumps({"login": login, "password": password}).encode()
    request = urllib.request.Request(
        f"{base}/api/auth/login", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = response.headers.get("Set-Cookie", "").split(";", 1)[0]
    if not value.startswith("lct_session="):
        raise RuntimeError("сервер не выдал lct_session cookie")
    return value


async def control(ws_base: str, session_id: str, cookie: str, payload: dict) -> None:
    header_arg = ("additional_headers" if "additional_headers" in inspect.signature(
        websockets.connect
    ).parameters else "extra_headers")
    async with websockets.connect(
        f"{ws_base}/ws/control/{session_id}",
        **{header_arg: {"Cookie": cookie}}, open_timeout=10, ping_interval=None,
    ) as socket:
        await socket.send(json.dumps(payload, ensure_ascii=False))
        await asyncio.sleep(0.03)


async def run(args: argparse.Namespace) -> int:
    frontend = local_url(args.frontend_url, {"http", "https"})
    backend = local_url(args.backend_url, {"http"})
    if args.sessions < 1 or args.clients < args.sessions or args.clients % args.sessions:
        raise ValueError("clients должно делиться на sessions и быть не меньше sessions")
    if args.clients > 100 or args.sessions > 20:
        raise ValueError("предохранитель: максимум 100 браузеров и 20 занятий")
    if args.ramp_seconds < 0:
        raise ValueError("ramp-seconds не может быть отрицательным")

    cookie = login_cookie(backend, args.login, args.password)
    cookie_name, cookie_value = cookie.split("=", 1)
    ws_base = "ws" + backend.removeprefix("http")
    sessions = [str(uuid4()) for _ in range(args.sessions)]
    started: list[str] = []
    try:
        await asyncio.gather(*(
            control(ws_base, session_id, cookie, {
                "type": "scenario.start", "scenario_id": args.scenario,
                "trainee": f"browser-load-{index:02d}", "mode": "training", "exercise": "dds",
            }) for index, session_id in enumerate(sessions)
        ))
        started.extend(sessions)

        timings: list[float] = []
        dds_timings: list[float] = []
        observer_timings: list[float] = []
        browser_navigation: list[dict] = []
        errors: list[str] = []
        pages = []
        contexts = []
        per_session = args.clients // args.sessions
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            for index in range(args.clients):
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    ignore_https_errors=args.ignore_https_errors,
                )
                await context.add_cookies([{
                    "name": cookie_name, "value": cookie_value, "url": frontend,
                    "httpOnly": True, "sameSite": "Lax",
                }])
                page = await context.new_page()
                page.on("pageerror", lambda error, i=index: errors.append(f"page {i}: {error}"))
                contexts.append(context)
                pages.append(page)

            async def open_page(index: int) -> None:
                if args.ramp_seconds and args.clients > 1:
                    await asyncio.sleep(args.ramp_seconds * index / (args.clients - 1))
                session_index = index // per_session
                role_index = index % per_session
                session_id = sessions[session_index]
                if role_index == 0:
                    path = f"/dds?session={session_id}&role=dds_01"
                    selector = ".dds-register-panel .dds-selected"
                else:
                    path = f"/wall?session={session_id}"
                    selector = "main.wall h1"
                started_at = time.perf_counter()
                try:
                    response = await pages[index].goto(
                        frontend + path, wait_until="domcontentloaded", timeout=args.timeout * 1000,
                    )
                    if response is None or not response.ok:
                        raise RuntimeError(f"HTTP {response.status if response else 'нет ответа'}")
                    await pages[index].locator(selector).wait_for(timeout=args.timeout * 1000)
                    if role_index != 0:
                        await pages[index].wait_for_function(
                            "document.querySelector('main.wall h1')?.textContent !== 'Ожидание занятия'",
                            timeout=args.timeout * 1000,
                        )
                    ready_ms = (time.perf_counter() - started_at) * 1000
                    timings.append(ready_ms)
                    (dds_timings if role_index == 0 else observer_timings).append(ready_ms)
                    navigation = await pages[index].evaluate("""() => {
                      const entry = performance.getEntriesByType('navigation')[0];
                      if (!entry) return null;
                      return {
                        responseStart: entry.responseStart,
                        responseEnd: entry.responseEnd,
                        domInteractive: entry.domInteractive,
                        domContentLoaded: entry.domContentLoadedEventEnd,
                        transferSize: entry.transferSize,
                      };
                    }""")
                    if navigation:
                        browser_navigation.append({"role": "dds" if role_index == 0 else "wall", **navigation})
                except Exception as exc:  # каждый незагрузившийся интерфейс — измеренный отказ
                    errors.append(f"page {index} {path}: {type(exc).__name__}: {exc}")

            await asyncio.gather(*(open_page(index) for index in range(args.clients)))
            await asyncio.sleep(args.hold_seconds)
            await asyncio.gather(*(context.close() for context in contexts))
            await browser.close()

        result = {
            "frontend": frontend, "backend": backend,
            "sessions_started": len(sessions), "browser_contexts": args.clients,
            "dds_pages": args.sessions, "observer_pages": args.clients - args.sessions,
            "pages_ready": len(timings), "hold_seconds": args.hold_seconds,
            "ramp_seconds": args.ramp_seconds,
            "ready_ms": timing_summary(timings),
            "ready_ms_by_page": {
                "dds": timing_summary(dds_timings),
                "wall": timing_summary(observer_timings),
            },
            "browser_navigation_ms": {
                key: timing_summary([float(item[key]) for item in browser_navigation])
                for key in ("responseStart", "responseEnd", "domInteractive", "domContentLoaded")
            },
            "transfer_bytes": timing_summary([
                float(item["transferSize"]) for item in browser_navigation
            ]),
            "errors": errors[:50],
            "pass_browser_ui_2s": (
                len(timings) == args.clients and not errors and percentile(timings, 95) <= 2000
            ),
            "scope": (
                f"{args.clients} isolated Chromium contexts render real React DDS/wall pages "
                f"for {args.sessions} sessions with a {args.ramp_seconds:g}s connection ramp; "
                "all contexts remain active concurrently; same instructor account, local host, no VoIP, "
                "not target 6-core/32-GB server"
            ),
        }
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        print(rendered)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        return 0 if result["pass_browser_ui_2s"] else 1
    finally:
        await asyncio.gather(*(
            control(ws_base, session_id, cookie, {"type": "session.stop"})
            for session_id in started
        ), return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5173")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--login", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--clients", type=int, default=100)
    parser.add_argument("--scenario", default="fire-apartment-l2")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--hold-seconds", type=float, default=2)
    parser.add_argument("--ramp-seconds", type=float, default=0,
                        help="равномерно распределить начало загрузок; все страницы остаются активны")
    parser.add_argument("--output", help="сохранить JSON-отчёт")
    parser.add_argument("--ignore-https-errors", action="store_true",
                        help="разрешить bootstrap self-signed сертификат только в локальном тесте")
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except Exception as exc:
        print(f"browser load failed: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
