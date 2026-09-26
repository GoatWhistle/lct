#!/usr/bin/env python3
"""Локальная браузерная проверка 20 занятий / 100 Chromium-контекстов."""

import argparse
import asyncio
import inspect
import json
import math
import secrets
import ssl
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4

import websockets
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


async def create_distinct_users(
    database_url: str, sessions: int, observers: int, *, create_controller: bool
) -> dict:
    """Provision short-lived local accounts for a literal 100-user load test."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.auth import hash_password
    from app.db.models import Trainee, User

    db_host = make_url(database_url).host
    if db_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("--distinct-users разрешён только для loopback PostgreSQL")

    suffix = uuid4().hex[:12]
    engine = create_async_engine(database_url)
    users: list[dict] = []
    trainee_ids: list[str] = []
    trainee_names: list[str] = []
    controller = None
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            if create_controller:
                controller = {
                    "login": f"load-i-{suffix}",
                    "password": secrets.token_urlsafe(24),
                }
                db.add(User(
                    login=controller["login"],
                    full_name=f"Нагрузочный преподаватель {suffix}",
                    password_hash=hash_password(controller["password"]),
                    role="instructor",
                    blocked=False,
                ))
            for index in range(sessions):
                name = f"Нагрузочный курсант {suffix}-{index:02d}"
                login = f"load-t-{suffix}-{index:02d}"
                password = secrets.token_urlsafe(24)
                trainee = Trainee(name=name)
                db.add(trainee)
                await db.flush()
                db.add(User(
                    login=login,
                    full_name=name,
                    password_hash=hash_password(password),
                    role="trainee",
                    trainee_id=trainee.id,
                    blocked=False,
                ))
                users.append({"login": login, "password": password})
                trainee_ids.append(str(trainee.id))
                trainee_names.append(name)
            for index in range(observers):
                login = f"load-a-{suffix}-{index:03d}"
                password = secrets.token_urlsafe(24)
                db.add(User(
                    login=login,
                    full_name=f"Нагрузочный наблюдатель {suffix}-{index:03d}",
                    password_hash=hash_password(password),
                    role="admin",
                    blocked=False,
                ))
                users.append({"login": login, "password": password})
            await db.commit()
    except Exception:
        await engine.dispose()
        raise
    await engine.dispose()
    return {
        "suffix": suffix,
        "users": users,
        "controller": controller,
        "trainee_ids": trainee_ids,
        "trainee_names": trainee_names,
    }


async def delete_distinct_users(database_url: str, fixtures: dict, session_ids: list[str]) -> None:
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.models import AuditLog, Session, Trainee, User

    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            ids = [UUID(value) for value in session_ids]
            logins = [item["login"] for item in fixtures["users"]]
            if fixtures["controller"]:
                logins.append(fixtures["controller"]["login"])
            if ids:
                await db.execute(delete(Session).where(Session.id.in_(ids)))
                await db.execute(delete(AuditLog).where(
                    AuditLog.object_id.in_([str(value) for value in ids])
                ))
            await db.execute(delete(AuditLog).where(AuditLog.actor.in_(logins)))
            await db.execute(delete(User).where(User.login.in_(logins)))
            await db.execute(delete(Trainee).where(
                Trainee.name.in_(fixtures["trainee_names"])
            ))
            await db.commit()
    finally:
        await engine.dispose()


async def session_node_distribution(database_url: str, session_ids: list[str]) -> dict[str, int]:
    """Read the durable node owner for this run's sessions before cleanup."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.models import Session

    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as db:
            rows = (await db.execute(
                select(Session.backend_node_id, func.count())
                .where(Session.id.in_([UUID(value) for value in session_ids]))
                .group_by(Session.backend_node_id)
            )).all()
            return {node or "<unassigned>": count for node, count in rows}
    finally:
        await engine.dispose()


def kill_cluster_service(project: str, service: str) -> None:
    """SIGKILL one backend only in an explicitly named disposable Compose project."""
    if not project or not project.startswith("lct-"):
        raise ValueError("fault injection requires an explicit lct-* disposable Compose project")
    listing = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
         "--filter", f"label=com.docker.compose.service={service}", "--format", "{{.ID}}"],
        check=False, capture_output=True, text=True,
    )
    containers = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
    if listing.returncode or len(containers) != 1:
        raise RuntimeError(
            f"expected exactly one running {service} in disposable project {project}; "
            f"found {len(containers)}"
        )
    completed = subprocess.run(
        ["docker", "kill", containers[0]], check=False, capture_output=True, text=True,
    )
    if completed.returncode:
        raise RuntimeError(f"Docker SIGKILL failed: {completed.stderr.strip()}")


async def wait_for_takeover(
    database_url: str, session_ids: list[str], *, failed_node: str, timeout: float,
) -> tuple[dict[str, int], float]:
    started = time.perf_counter()
    deadline = started + timeout
    distribution: dict[str, int] = {}
    while time.perf_counter() < deadline:
        distribution = await session_node_distribution(database_url, session_ids)
        if sum(distribution.values()) == len(session_ids) and failed_node not in distribution:
            return distribution, (time.perf_counter() - started) * 1000
        await asyncio.sleep(0.5)
    return distribution, (time.perf_counter() - started) * 1000


async def login_distinct_users(base: str, users: list[dict]) -> list[str]:
    semaphore = asyncio.Semaphore(8)

    async def login(user: dict) -> str:
        async with semaphore:
            return await asyncio.to_thread(login_cookie, base, user["login"], user["password"])

    return await asyncio.gather(*(login(user) for user in users))


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


async def control(
    ws_base: str, session_id: str, cookie: str, payload: dict, *, ignore_https_errors: bool = False
) -> None:
    header_arg = ("additional_headers" if "additional_headers" in inspect.signature(
        websockets.connect
    ).parameters else "extra_headers")
    options = {header_arg: {"Cookie": cookie}, "open_timeout": 10, "ping_interval": None}
    if ws_base.startswith("wss://") and ignore_https_errors:
        options["ssl"] = ssl._create_unverified_context()
    async with websockets.connect(
        f"{ws_base}/ws/control/{session_id}",
        **options,
    ) as socket:
        await socket.send(json.dumps(payload, ensure_ascii=False))
        await asyncio.sleep(0.03)


async def probe_call_ws(
    ws_base: str, session_id: str, cookie: str, *, ignore_https_errors: bool,
) -> dict:
    """Probe a new call-channel handshake without competing for Chromium sockets."""
    header_arg = ("additional_headers" if "additional_headers" in inspect.signature(
        websockets.connect
    ).parameters else "extra_headers")
    options = {
        header_arg: {"Cookie": cookie}, "open_timeout": 5, "ping_interval": None,
    }
    if ws_base.startswith("wss://") and ignore_https_errors:
        options["ssl"] = ssl._create_unverified_context()
    started = time.perf_counter()
    try:
        async with websockets.connect(f"{ws_base}/ws/call/{session_id}", **options):
            return {"ok": True, "elapsed_ms": (time.perf_counter() - started) * 1000}
    except Exception as exc:
        return {
            "ok": False,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "reason": f"{type(exc).__name__}: {str(exc)[:240]}",
        }


def track_page_websockets(page, records: list[dict]) -> None:
    """Record only frame/close timestamps; never retain payloads or user data."""
    def on_websocket(socket) -> None:
        record = {
            "url": socket.url,
            "created_at": time.perf_counter(),
            "frames": [],
            "closed_at": None,
        }
        records.append(record)

        def on_frame(frame, item=record) -> None:
            event_type = None
            error_code = None
            if isinstance(frame, str):
                try:
                    value = json.loads(frame)
                    if isinstance(value, dict):
                        event_type = value.get("type")
                        error_code = value.get("code")
                except json.JSONDecodeError:
                    pass
            item["frames"].append({
                "at": time.perf_counter(), "type": event_type, "code": error_code,
            })

        socket.on("framereceived", on_frame)
        socket.on("close", lambda _socket, item=record: item.update(
            closed_at=time.perf_counter()
        ))

    page.on("websocket", on_websocket)


def browser_channels_recovered(
    records_by_page: list[list[dict]], expected_paths: list[str], after: float,
) -> tuple[list[int], list[float]]:
    recovered: list[int] = []
    first_frame_after: list[float] = []
    for index, records in enumerate(records_by_page):
        candidates = []
        for record in records:
            if expected_paths[index] not in record["url"]:
                continue
            post_failure_frames = [
                frame["at"] for frame in record["frames"]
                if frame["at"] >= after and frame["type"] is not None
                and frame["type"] != "error" and frame["code"] is None
            ]
            if (post_failure_frames
                    and (record["closed_at"] is None
                         or record["closed_at"] > post_failure_frames[-1])):
                candidates.append(post_failure_frames[0])
        if candidates:
            recovered.append(index)
            first_frame_after.append(min(candidates))
    return recovered, first_frame_after


async def wait_for_browser_channels(
    records_by_page: list[list[dict]], expected_paths: list[str], after: float, timeout: float,
) -> tuple[list[int], list[float]]:
    deadline = time.perf_counter() + max(0, timeout)
    while True:
        recovered, first_frames = browser_channels_recovered(
            records_by_page, expected_paths, after,
        )
        if len(recovered) == len(records_by_page) or time.perf_counter() >= deadline:
            return recovered, first_frames
        await asyncio.sleep(0.1)


async def run(args: argparse.Namespace) -> int:
    frontend = local_url(args.frontend_url, {"http", "https"})
    backend = local_url(args.backend_url, {"http"})
    control_origin = local_url(args.control_url or args.backend_url, {"http", "https"})
    if bool(args.login) != bool(args.password):
        raise ValueError("--login и --password задаются вместе")
    if not args.distinct_users and not args.login:
        raise ValueError("обычный режим требует --login и --password")
    if args.sessions < 1 or args.clients < args.sessions or args.clients % args.sessions:
        raise ValueError("clients должно делиться на sessions и быть не меньше sessions")
    if args.clients > 100 or args.sessions > 20:
        raise ValueError("предохранитель: максимум 100 браузеров и 20 занятий")
    if args.distinct_users and (args.sessions != 20 or args.clients != 100):
        raise ValueError("--distinct-users проверяет ровно 20 занятий и 100 отдельных пользователей")
    if args.ramp_seconds < 0:
        raise ValueError("ramp-seconds не может быть отрицательным")
    if not 0 <= args.expect_backend_nodes <= args.sessions:
        raise ValueError("expect-backend-nodes должен быть от 0 до числа занятий")
    if args.kill_backend_b_after_pages:
        if not args.distinct_users or args.sessions != 20 or args.clients != 100:
            raise ValueError("cluster fault-injection requires --distinct-users with 20 sessions / 100 clients")
        if not args.database_url or not args.compose_project:
            raise ValueError("cluster fault-injection requires --database-url and --compose-project")
        if args.expect_backend_nodes < 2:
            raise ValueError("fault-injection requires --expect-backend-nodes 2")
        if control_origin != frontend or not frontend.startswith("https://"):
            raise ValueError("fault-injection requires TLS Nginx for both frontend and control-url")
        if args.failure_timeout <= 0:
            raise ValueError("failure-timeout must be positive")

    control_ws_base = control_origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    sessions = [str(uuid4()) for _ in range(args.sessions)]
    started: list[str] = []
    fixtures = None
    teacher_cookie = None
    try:
        station_cookies: list[str] = []
        observer_cookies: list[str] = []
        if args.distinct_users:
            if not args.database_url:
                raise ValueError("--distinct-users требует --database-url")
            fixtures = await create_distinct_users(
                args.database_url,
                args.sessions,
                args.clients - args.sessions,
                create_controller=not bool(args.login),
            )
            controller = fixtures["controller"]
            teacher_cookie = (
                login_cookie(backend, controller["login"], controller["password"])
                if controller
                else login_cookie(backend, args.login, args.password)
            )
            account_cookies = await login_distinct_users(backend, fixtures["users"])
            station_cookies = account_cookies[:args.sessions]
            observer_cookies = account_cookies[args.sessions:]
        else:
            if not args.login or not args.password:
                raise ValueError("обычный режим требует --login и --password")
            teacher_cookie = login_cookie(backend, args.login, args.password)

        # Include partially started sessions in cleanup if one concurrent
        # control connection fails midway through the preparation batch.
        started.extend(sessions)
        await asyncio.gather(*(
            control(control_ws_base, session_id, teacher_cookie, {
                "type": "scenario.start", "scenario_id": args.scenario,
                "trainee": (
                    fixtures["trainee_names"][index] if fixtures
                    else f"browser-load-{index:02d}"
                ),
                "trainee_id": fixtures["trainee_ids"][index] if fixtures else None,
                "mode": "training", "exercise": "dds",
            }, ignore_https_errors=args.ignore_https_errors)
            for index, session_id in enumerate(sessions)
        ))

        timings: list[float] = []
        dds_timings: list[float] = []
        observer_timings: list[float] = []
        browser_navigation: list[dict] = []
        page_readiness_profiles: list[dict] = []
        errors: list[str] = []
        pages = []
        contexts = []
        per_session = args.clients // args.sessions
        observer_index = 0
        failure_injection = None
        pre_failure_distribution: dict[str, int] = {}
        websocket_records: list[list[dict]] = [[] for _ in range(args.clients)]
        expected_ws_paths: list[str] = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            for index in range(args.clients):
                session_index = index // per_session
                role_index = index % per_session
                if fixtures:
                    account_cookie = (
                        station_cookies[session_index]
                        if role_index == 0
                        else observer_cookies[observer_index]
                    )
                    if role_index != 0:
                        observer_index += 1
                else:
                    account_cookie = teacher_cookie
                cookie_name, cookie_value = account_cookie.split("=", 1)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 720},
                    ignore_https_errors=args.ignore_https_errors,
                )
                await context.add_init_script("""
                  window.__lctLongTasks = [];
                  if (typeof PerformanceObserver !== "undefined") {
                    try {
                      new PerformanceObserver((list) => {
                        for (const item of list.getEntries()) {
                          window.__lctLongTasks.push({ start: item.startTime, duration: item.duration });
                        }
                      }).observe({ type: "longtask", buffered: true });
                    } catch (_) {}
                  }
                """)
                await context.add_cookies([{
                    "name": cookie_name, "value": cookie_value, "url": frontend,
                    "httpOnly": True, "sameSite": "Lax",
                }])
                page = await context.new_page()
                page.on("pageerror", lambda error, i=index: errors.append(f"page {i}: {error}"))
                session_index = index // per_session
                role_index = index % per_session
                session_id = sessions[session_index]
                expected_ws_paths.append(
                    f"/ws/station/{session_id}" if role_index == 0
                    else f"/ws/observe/{session_id}"
                )
                track_page_websockets(page, websocket_records[index])
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
                    document_ready_ms = (time.perf_counter() - started_at) * 1000
                    await pages[index].locator(selector).wait_for(timeout=args.timeout * 1000)
                    if role_index != 0:
                        await pages[index].wait_for_function(
                            "document.querySelector('main.wall h1')?.textContent !== 'Ожидание занятия'",
                            timeout=args.timeout * 1000,
                        )
                    ready_ms = (time.perf_counter() - started_at) * 1000
                    timings.append(ready_ms)
                    (dds_timings if role_index == 0 else observer_timings).append(ready_ms)
                    profile = await pages[index].evaluate("""() => {
                      const resources = performance.getEntriesByType("resource")
                        .map((item) => {
                          const path = new URL(item.name).pathname.replace(
                            /[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/gi, ":id"
                          );
                          return {
                            path,
                            initiator: item.initiatorType,
                            duration_ms: item.duration,
                            transfer_bytes: item.transferSize
                          };
                        })
                        .filter((item) => item.path.startsWith("/assets/")
                          || item.path.startsWith("/api/"))
                        .sort((left, right) => right.duration_ms - left.duration_ms)
                        .slice(0, 30);
                      const tasks = window.__lctLongTasks ?? [];
                      return {
                        long_tasks: tasks.map((item) => ({
                          start_ms: item.start, duration_ms: item.duration
                        })),
                        resources
                      };
                    }""")
                    profile["websockets"] = []
                    for socket in websocket_records[index]:
                        socket_path = urllib.parse.urlsplit(socket["url"]).path
                        if socket_path.startswith("/ws/station/"):
                            socket_path = "/ws/station/:id"
                        elif socket_path.startswith("/ws/observe/"):
                            socket_path = "/ws/observe/:id"
                        elif socket_path.startswith("/ws/control/"):
                            socket_path = "/ws/control/:id"
                        frame_types = [frame["type"] for frame in socket["frames"]]
                        first_state = next((
                            frame for frame in socket["frames"]
                            if frame["type"] in {
                                "station.state", "observe.state", "session.snapshot",
                                "card.received",
                            }
                        ), None)
                        profile["websockets"].append({
                            "path": socket_path,
                            "created_after_navigation_ms": round(
                                (socket["created_at"] - started_at) * 1000, 2
                            ),
                            "first_frame_after_navigation_ms": round(
                                (socket["frames"][0]["at"] - started_at) * 1000, 2
                            ) if socket["frames"] else None,
                            "first_state_after_navigation_ms": round(
                                (first_state["at"] - started_at) * 1000, 2
                            ) if first_state else None,
                            "first_frame_type": frame_types[0] if frame_types else None,
                        })
                    page_readiness_profiles.append({
                        "page_index": index,
                        "role": "dds" if role_index == 0 else "wall",
                        "navigation_to_domcontentloaded_ms": round(document_ready_ms, 2),
                        "domcontentloaded_to_ready_ms": round(ready_ms - document_ready_ms, 2),
                        **profile,
                    })
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

            session_api_ok = 0
            call_ws_ok = 0
            route_errors: list[str] = []
            if args.kill_backend_b_after_pages:
                if len(timings) != args.clients or errors:
                    raise RuntimeError("all 100 pages must be ready before failure injection")
                initial_ws_connected = sum(
                    any(
                        expected_ws_paths[index] in record["url"]
                        and any(
                            frame["type"] is not None
                            and frame["type"] != "error" and frame["code"] is None
                            for frame in record["frames"]
                        )
                        for record in records
                    )
                    for index, records in enumerate(websocket_records)
                )
                if initial_ws_connected != args.clients:
                    raise RuntimeError(
                        f"only {initial_ws_connected}/{args.clients} pages received their initial WS frame"
                    )
                pre_failure_distribution = await session_node_distribution(
                    args.database_url, sessions,
                )
                if set(pre_failure_distribution) != {"backend-a", "backend-b"}:
                    raise RuntimeError(
                        "pre-failure sessions must be owned by both backend-a and backend-b"
                    )
                failure_started = time.perf_counter()
                kill_cluster_service(args.compose_project, "backend-b")
                post_failure_distribution, takeover_ms = await wait_for_takeover(
                    args.database_url, sessions, failed_node="backend-b",
                    timeout=args.failure_timeout,
                )
                recovery_budget = max(
                    0.0,
                    min(30.0, args.failure_timeout) - (time.perf_counter() - failure_started),
                )
                recovered_pages, first_frames = await wait_for_browser_channels(
                    websocket_records, expected_ws_paths, failure_started, recovery_budget,
                )
                failure_injection = {
                    "action": "SIGKILL backend-b after all pages became ready",
                    "compose_project": args.compose_project,
                    "owner_distribution_before": pre_failure_distribution,
                    "owner_distribution_after": post_failure_distribution,
                    "takeover_ms": round(takeover_ms, 2),
                    "concurrent_pages_kept_open_during_takeover": args.clients,
                    "takeover_passed": (
                        sum(post_failure_distribution.values()) == args.sessions
                        and post_failure_distribution.get("backend-a") == args.sessions
                        and "backend-b" not in post_failure_distribution
                    ),
                    "browser_ws_channel_pages_recovered": len(recovered_pages),
                    "browser_ws_channel_pages_expected": args.clients,
                    "browser_ws_initial_pages_connected": initial_ws_connected,
                    "browser_ws_unrecovered_pages": [
                        {
                            "page_index": index,
                            "channel": expected_ws_paths[index],
                            "session_id": sessions[index // per_session],
                        }
                        for index in range(args.clients) if index not in recovered_pages
                    ],
                    "browser_ws_channels_recovered_within_30s": (
                        len(recovered_pages) == args.clients
                        and bool(first_frames)
                        and max(first_frames) - failure_started <= 30.0
                    ),
                    "browser_ws_recovery_ms": (
                        round((max(first_frames) - failure_started) * 1000, 2)
                        if len(recovered_pages) == args.clients else None
                    ),
                }
                failure_injection["elapsed_since_kill_ms"] = round(
                    (time.perf_counter() - failure_started) * 1000, 2
                )
                mounted = await asyncio.gather(*(
                    page.evaluate("Boolean(document.querySelector('#root')?.firstElementChild)")
                    for page in pages
                ))
                failure_injection["react_pages_still_mounted"] = sum(bool(item) for item in mounted)

            async def probe_session_api(session_index: int) -> dict:
                page = pages[session_index * per_session]
                return await page.evaluate("""async (sessionId) => {
                  const started = performance.now();
                  const response = await fetch(`/api/sessions/${sessionId}`, {
                    credentials: "same-origin"
                  });
                  return {
                    ok: response.status === 200,
                    status: response.status,
                    elapsed_ms: performance.now() - started
                  };
                }""", sessions[session_index])

            api_probes = await asyncio.gather(*(
                probe_session_api(session_index) for session_index in range(args.sessions)
            ))
            ws_probe_cookies = (
                station_cookies if fixtures else [teacher_cookie] * args.sessions
            )
            ws_probes = await asyncio.gather(*(
                probe_call_ws(
                    control_ws_base, sessions[index], ws_probe_cookies[index],
                    ignore_https_errors=args.ignore_https_errors,
                )
                for index in range(args.sessions)
            ))
            if failure_injection:
                failure_injection["route_probe_ms"] = {
                    "session_api_max": round(max(item["elapsed_ms"] for item in api_probes), 2),
                    "call_ws_max": round(max(item["elapsed_ms"] for item in ws_probes), 2),
                    "call_ws_probe_client": "Python websockets; Chromium pages remain open",
                }
            for session_index, (api_probe, ws_probe) in enumerate(zip(api_probes, ws_probes)):
                if api_probe["ok"]:
                    session_api_ok += 1
                else:
                    route_errors.append(
                        f"session {sessions[session_index]}: GET session returned {api_probe['status']}"
                    )
                if ws_probe["ok"]:
                    call_ws_ok += 1
                else:
                    route_errors.append(
                        f"session {sessions[session_index]}: /ws/call route failed: {ws_probe}"
                    )

            await asyncio.sleep(args.hold_seconds)
            await asyncio.gather(*(context.close() for context in contexts))
            await browser.close()

        node_distribution = (
            await session_node_distribution(args.database_url, sessions)
            if args.database_url else {}
        )
        nodes_for_expectation = pre_failure_distribution if failure_injection else node_distribution
        expected_nodes_ready = (
            len(nodes_for_expectation) >= args.expect_backend_nodes
            if args.expect_backend_nodes else True
        )
        if failure_injection:
            failure_injection["session_rest_routes_recovered"] = session_api_ok == args.sessions
            failure_injection["call_ws_routes_recovered"] = call_ws_ok == args.sessions
            failure_injection["route_errors_after_takeover"] = list(route_errors)
            failure_injection["all_probes_recovered"] = (
                session_api_ok == args.sessions and call_ws_ok == args.sessions
                and not route_errors
            )
            failure_injection["pass_20_session_takeover"] = (
                failure_injection["takeover_passed"]
                and failure_injection["all_probes_recovered"]
                and failure_injection["browser_ws_channels_recovered_within_30s"]
                and failure_injection["react_pages_still_mounted"] == args.clients
            )
        result = {
            "frontend": frontend, "backend": backend, "control": control_origin,
            "sessions_started": len(sessions), "browser_contexts": args.clients,
            "dds_pages": args.sessions, "observer_pages": args.clients - args.sessions,
            "distinct_authenticated_users": (
                args.clients if fixtures else 1
            ),
            "distinct_users_mode": bool(fixtures),
            "backend_node_distribution": node_distribution,
            "backend_node_count": len(node_distribution),
            "backend_node_distribution_before_failure": (
                pre_failure_distribution if failure_injection else None
            ),
            "failure_injection": failure_injection,
            "session_rest_routes_ok": session_api_ok,
            "call_ws_routes_ok": call_ws_ok,
            "route_errors": route_errors,
            "pages_ready": len(timings), "hold_seconds": args.hold_seconds,
            "ramp_seconds": args.ramp_seconds,
            "ready_ms": timing_summary(timings),
            "ready_ms_by_page": {
                "dds": timing_summary(dds_timings),
                "wall": timing_summary(observer_timings),
            },
            "page_readiness_profiles": page_readiness_profiles,
            "pass_wall_report_deferred": not any(
                path == "/api/sessions/:id/report"
                or path.startswith("/assets/Debrief-")
                or path.startswith("/assets/Radar-")
                for profile in page_readiness_profiles if profile.get("role") == "wall"
                for path in (resource.get("path", "") for resource in profile.get("resources", []))
            ),
            "browser_navigation_ms": {
                key: timing_summary([float(item[key]) for item in browser_navigation])
                for key in ("responseStart", "responseEnd", "domInteractive", "domContentLoaded")
            },
            "transfer_bytes": timing_summary([
                float(item["transferSize"]) for item in browser_navigation
            ]),
            "errors": errors[:50],
            "pass_browser_ui_2s": (
                len(timings) == args.clients
                and not errors
                and not route_errors
                and session_api_ok == args.sessions
                and call_ws_ok == args.sessions
                and percentile(timings, 95) <= 2000
                and len(dds_timings) == args.sessions
                and len(observer_timings) == args.clients - args.sessions
                and percentile(dds_timings, 95) <= 2000
                and percentile(observer_timings, 95) <= 2000
                and expected_nodes_ready
            ),
            "pass_failover_under_20x100": bool(
                failure_injection and failure_injection["pass_20_session_takeover"]
            ) if args.kill_backend_b_after_pages else None,
            "scope": (
                f"{args.clients} isolated Chromium contexts render real React DDS/wall pages "
                f"for {args.sessions} sessions with a {args.ramp_seconds:g}s connection ramp; "
                "all contexts remain active concurrently; "
                + ("unique trainee/admin accounts" if fixtures else "same instructor account")
                + ", local host, no VoIP, "
                "not target 6-core/32-GB server"
                + (f"; at least {args.expect_backend_nodes} backend nodes required"
                   if args.expect_backend_nodes else "")
            ),
        }
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        print(rendered)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as stream:
                stream.write(rendered + "\n")
        passed = result["pass_browser_ui_2s"] and (
            result["pass_wall_report_deferred"]
            and result["pass_failover_under_20x100"] is not False
        )
        return 0 if passed else 1
    finally:
        try:
            if teacher_cookie and started:
                await asyncio.gather(*(
                    control(control_ws_base, session_id, teacher_cookie, {"type": "session.stop"},
                            ignore_https_errors=args.ignore_https_errors)
                    for session_id in started
                ), return_exceptions=True)
        finally:
            if fixtures:
                await delete_distinct_users(args.database_url, fixtures, started)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-url", default="http://127.0.0.1:5173")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--control-url",
                        help="URL для управляющего WS; задайте frontend URL для проверки proxy-affinity")
    parser.add_argument("--login", help="преподаватель; в --distinct-users создаётся временный")
    parser.add_argument("--password", help="пароль преподавателя, если задан --login")
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--clients", type=int, default=100)
    parser.add_argument("--scenario", default="fire-apartment-l2")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--hold-seconds", type=float, default=2)
    parser.add_argument("--ramp-seconds", type=float, default=0,
                        help="равномерно распределить начало загрузок; все страницы остаются активны")
    parser.add_argument("--distinct-users", action="store_true",
                        help="создать/удалить временные учётки и проверить разные логины")
    parser.add_argument("--database-url",
                        help="PostgreSQL URL, обязателен для --distinct-users; только тестовая БД")
    parser.add_argument("--expect-backend-nodes", type=int, default=0,
                        help="проверить, что занятия распределены минимум по N backend-узлам")
    parser.add_argument("--kill-backend-b-after-pages", action="store_true",
                        help="SIGKILL backend-b after all 100 pages load; isolated cluster only")
    parser.add_argument("--compose-project",
                        help="required unique lct-* project for --kill-backend-b-after-pages")
    parser.add_argument("--failure-timeout", type=float, default=45,
                        help="maximum seconds to wait for leases/checkpoints to transfer")
    parser.add_argument("--output", help="сохранить JSON-отчёт")
    parser.add_argument("--ignore-https-errors", action="store_true",
                        help="разрешить bootstrap self-signed сертификат только в локальном тесте")
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except Exception as exc:
        print(f"browser load failed: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
