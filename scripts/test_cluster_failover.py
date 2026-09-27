#!/usr/bin/env python3
"""Live smoke: verify a complete DDS exercise across fenced cluster takeover.

Only use with a disposable local Compose cluster and its own PostgreSQL database.
The script creates temporary users/session, stops and restarts one named backend,
and removes the temporary rows on completion.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import json
import os
import secrets
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4

import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def compose(args: argparse.Namespace, *command: str, check: bool = True) -> str:
    env = os.environ.copy()
    env.update({
        "POSTGRES_PORT": str(args.postgres_port),
        "BACKEND_PORT": str(args.backend_port),
        "BACKEND_B_PORT": str(args.backend_b_port),
        "FRONTEND_PORT": str(args.frontend_port),
        "TLS_PORT": str(args.tls_port),
        "POSTGRES_PASSWORD": args.postgres_password,
        "SESSION_SECRET": args.session_secret,
        "TLS_CERT_DIR": args.tls_cert_dir,
    })
    files = [
        "docker-compose.yml", "docker-compose.tls.yml",
        "docker-compose.load-test.yml", "docker-compose.cluster.yml",
    ]
    if args.failure_mode == "partition":
        files.append("docker-compose.partition-test.yml")
    result = subprocess.run(
        ["docker", "compose", "-p", args.project,
         *[part for file in files for part in ("-f", str(ROOT / file))], *command],
        cwd=ROOT, env=env, check=False, capture_output=True, text=True,
    )
    if check and result.returncode:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, output=result.stdout, stderr=result.stderr
        )
    return result.stdout


def login(base: str, login_name: str, password: str) -> str:
    payload = json.dumps({"login": login_name, "password": password}).encode()
    request = urllib.request.Request(
        f"{base}/api/auth/login", data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        cookie = response.headers.get("Set-Cookie", "").split(";", 1)[0]
    if not cookie.startswith("lct_session="):
        raise RuntimeError("login did not issue lct_session cookie")
    return cookie


async def send_control(ws_base: str, session_id: UUID, cookie: str, payload: dict) -> None:
    async with websockets.connect(
        f"{ws_base}/ws/control/{session_id}",
        additional_headers={"Cookie": cookie},
        ssl=ssl._create_unverified_context() if ws_base.startswith("wss://") else None,
        open_timeout=10,
        ping_interval=None,
    ) as socket:
        await socket.send(json.dumps(payload, ensure_ascii=False))
        await asyncio.sleep(0.15)


async def probe_ws(ws_base: str, path: str, cookie: str) -> dict:
    try:
        async with websockets.connect(
            f"{ws_base}{path}", additional_headers={"Cookie": cookie},
            ssl=ssl._create_unverified_context() if ws_base.startswith("wss://") else None,
            open_timeout=6, ping_interval=None,
        ) as socket:
            try:
                message = await asyncio.wait_for(socket.recv(), timeout=4)
                event = json.loads(message) if isinstance(message, str) else "binary"
            except TimeoutError:
                event = "connected_no_initial_event"
            return {"connected": True, "initial_event": event}
    except Exception as exc:  # report exact endpoint failure in the result
        return {"connected": False, "error": f"{type(exc).__name__}: {exc}"}


async def station_command(
    ws_base: str, session_id: UUID, cookie: str, payload: dict | None = None,
) -> dict:
    """Read the authoritative station snapshot, optionally issue one command."""
    async with websockets.connect(
        f"{ws_base}/ws/station/{session_id}", additional_headers={"Cookie": cookie},
        ssl=ssl._create_unverified_context() if ws_base.startswith("wss://") else None,
        open_timeout=10, ping_interval=None,
    ) as socket:
        async def receive_snapshot() -> dict:
            for _ in range(60):
                raw = await asyncio.wait_for(socket.recv(), timeout=10)
                event = json.loads(raw) if isinstance(raw, str) else {}
                if event.get("type") == "error":
                    raise RuntimeError(f"station rejected smoke: {event.get('message')}")
                if event.get("type") == "station.state":
                    return event["snapshot"]
            raise RuntimeError("station did not send its state snapshot")

        snapshot = await receive_snapshot()
        if payload is None:
            return snapshot
        await socket.send(json.dumps(payload, ensure_ascii=False))
        return await receive_snapshot()


async def replay_station_command(
    ws_base: str, session_id: UUID, cookie: str, payload: dict, command_id: str,
    attempts: int = 5,
) -> bool:
    """Replay one already committed command and require its durable ACK."""
    last_error = None
    for attempt in range(attempts):
        try:
            async with websockets.connect(
                f"{ws_base}/ws/station/{session_id}", additional_headers={"Cookie": cookie},
                ssl=ssl._create_unverified_context() if ws_base.startswith("wss://") else None,
                open_timeout=10, ping_interval=None,
            ) as socket:
                for _ in range(60):
                    raw = await asyncio.wait_for(socket.recv(), timeout=10)
                    event = json.loads(raw) if isinstance(raw, str) else {}
                    if event.get("type") == "error":
                        raise RuntimeError(f"station reconnect rejected: {event.get('message')}")
                    if event.get("type") == "station.state":
                        break
                else:
                    raise RuntimeError("replacement station did not send a state snapshot")
                replay = {**payload, "_command_id": command_id}
                await socket.send(json.dumps(replay, ensure_ascii=False))
                for _ in range(60):
                    raw = await asyncio.wait_for(socket.recv(), timeout=10)
                    event = json.loads(raw) if isinstance(raw, str) else {}
                    if event.get("type") == "error":
                        raise RuntimeError(f"station replay rejected: {event.get('message')}")
                    if event.get("type") == "command.ack":
                        return event.get("command_id") == command_id
                raise RuntimeError("replacement owner did not acknowledge replayed command")
        except (TimeoutError, OSError, websockets.exceptions.WebSocketException) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(1)
    raise RuntimeError(f"station replay did not reconnect: {last_error}")


def ws_connect(ws_base: str, path: str, cookie: str):
    return websockets.connect(
        f"{ws_base}{path}", additional_headers={"Cookie": cookie},
        ssl=ssl._create_unverified_context() if ws_base.startswith("wss://") else None,
        open_timeout=8, ping_interval=None,
    )


async def wait_for_fence(socket, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = await asyncio.wait_for(socket.recv(), timeout=deadline - time.monotonic())
        except TimeoutError:
            return {"closed": False, "fence_notice": False, "reason": "timeout"}
        except Exception as exc:
            close = getattr(exc, "rcvd", None) or getattr(exc, "sent", None)
            close_code = getattr(close, "code", None) or getattr(exc, "code", None)
            return {
                "closed": True,
                "fence_notice": close_code == 1012,
                "close_code": close_code,
                "reason": getattr(close, "reason", "") if close else str(exc),
            }
        if isinstance(message, str):
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                continue
            if event.get("message") == "Занятие передано другому backend-узлу; переподключитесь.":
                return {"closed": True, "fence_notice": True, "close_code": 1012,
                        "reason": event.get("message")}
    return {"closed": False, "fence_notice": False, "reason": "timeout"}


async def main(args: argparse.Namespace) -> int:
    from sqlalchemy import delete, select
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.api.auth import hash_password
    from app.db.models import AuditLog, Session, Trainee, User

    database_url = (
        f"postgresql+asyncpg://lct:{args.postgres_password}@127.0.0.1:"
        f"{args.postgres_port}/lct"
    )
    if make_url(database_url).host not in {"127.0.0.1", "localhost"}:
        raise ValueError("failover smoke accepts loopback PostgreSQL only")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    observer_engine = create_async_engine(database_url, poolclass=NullPool)
    run_id = uuid4().hex[:12]
    session_id = uuid4()
    trainee = Trainee(name=f"Failover smoke {run_id}")
    instructor_login = f"fo-i-{run_id}"
    trainee_login = f"fo-t-{run_id}"
    instructor_password = secrets.token_urlsafe(24)
    trainee_password = secrets.token_urlsafe(24)
    owner_service = "backend"
    owner_stopped = False
    fault_service = None
    fault_stopped = False
    result: dict = {"session_id": str(session_id), "checks": {}}
    backend = f"http://127.0.0.1:{args.backend_port}"
    ws_base = args.frontend_url.replace("https://", "wss://").replace("http://", "ws://")
    instructor_cookie = trainee_cookie = None
    try:
        async with factory() as db:
            db.add(trainee)
            await db.flush()
            db.add_all([
                User(login=instructor_login, full_name="Failover smoke instructor",
                     password_hash=hash_password(instructor_password), role="instructor",
                     blocked=False),
                User(login=trainee_login, full_name=trainee.name,
                     password_hash=hash_password(trainee_password), role="trainee",
                     trainee_id=trainee.id, blocked=False),
            ])
            await db.commit()

        instructor_cookie = login(backend, instructor_login, instructor_password)
        trainee_cookie = login(backend, trainee_login, trainee_password)
        # Учётки созданы прямо в БД, вход — на узле A, а сокет занятия Nginx
        # может отдать узлу B. B узнаёт новый логин при очередной сверке
        # поколений (раз в секунду); до неё middleware считает cookie отозванной.
        await asyncio.sleep(2.5)
        await send_control(ws_base, session_id, instructor_cookie, {
            "type": "scenario.start", "scenario_id": args.scenario,
            "trainee": trainee.name, "trainee_id": str(trainee.id),
            "mode": "training", "exercise": "dds",
        })

        async def session_row():
            async with factory() as db:
                return await db.scalar(select(Session).where(Session.id == session_id))

        async def wait_for_command_checkpoint(command_id: str, timeout: float = 10) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                # Use a new physical connection, avoiding a pooled observer's
                # stale read transaction while the websocket writer commits.
                async with observer_engine.connect() as db:
                    payload = (await db.execute(
                        select(Session.live_state).where(Session.id == session_id)
                    )).scalar_one_or_none()
                if payload and command_id in payload.get("processed_station_commands", []):
                    return True
                await asyncio.sleep(0.1)
            return False

        deadline = time.monotonic() + 12
        row = None
        while time.monotonic() < deadline:
            row = await session_row()
            if row and row.backend_node_id and row.checkpoint_at and row.live_state:
                break
            await asyncio.sleep(0.2)
        if not row or not row.live_state:
            raise RuntimeError("session did not persist an owner and recovery checkpoint")
        if row.backend_node_id not in {"backend-a", "backend-b"}:
            raise RuntimeError(f"unexpected session owner: {row.backend_node_id}")
        owner_service = "backend" if row.backend_node_id == "backend-a" else "backend-b"
        old_epoch = row.backend_fencing_epoch
        result["initial_owner"] = row.backend_node_id
        result["initial_epoch"] = old_epoch

        # Commit real DDS work before the fault. These actions must survive the
        # checkpoint handoff and remain part of the final scored report.
        snapshot = await station_command(ws_base, session_id, trainee_cookie)
        service = snapshot.get("managed_service") or next(iter(snapshot["services"]), None)
        if not service:
            raise RuntimeError("the DDS exercise has no managed service")
        crew = next(
            (item for item in snapshot["crew_options"] if item.startswith(service + " — ")),
            None,
        )
        if not crew:
            raise RuntimeError(f"no crew option is available for {service}")
        steps_before_fault = []
        for status, detail in (
            ("accepted", "card accepted for processing"),
            ("crew.select", crew),
            ("responding", "crew reported departure"),
        ):
            if status == "crew.select":
                command = {"type": status, "crew": crew}
            else:
                command = {
                    "type": "card.status", "service": service, "status": status,
                    "comment": f"Основание: доклад по карточке.\nСведения: {detail}; {service} notified.",
                }
            snapshot = await station_command(ws_base, session_id, trainee_cookie, command)
            steps_before_fault.append(status)
        result["dds_progress_before_fault"] = steps_before_fault
        result["dds_service"] = service
        result["dds_crew"] = crew

        # Commit a non-replace station command, but close the client socket
        # without reading command.ack. The live database read proves the ID
        # and business state are committed before the owner fails.
        lost_ack_command_id = str(uuid4())
        lost_ack_command = {
            "type": "card.status", "service": service, "status": "arrived",
            "comment": (
                "Основание: доклад по карточке.\n"
                f"Сведения: прибытие до отказа; {service} notified."
            ),
        }
        async with ws_connect(
            ws_base, f"/ws/station/{session_id}", trainee_cookie,
        ) as lost_ack_socket:
            for _ in range(60):
                raw = await asyncio.wait_for(lost_ack_socket.recv(), timeout=10)
                event = json.loads(raw) if isinstance(raw, str) else {}
                if event.get("type") == "station.state":
                    break
                if event.get("type") == "error":
                    raise RuntimeError(f"station rejected lost-ACK setup: {event.get('message')}")
            else:
                raise RuntimeError("station did not become ready for lost-ACK command")
            await lost_ack_socket.send(json.dumps({
                **lost_ack_command, "_command_id": lost_ack_command_id,
            }, ensure_ascii=False))
            # Observe protocol output in the harness, but deliberately do not
            # dispatch the ACK to the simulated browser/outbox. This is the
            # lost-ack boundary: the command is committed, client state stays
            # pending, and the socket is closed before the application consumes
            # command.ack.
            deadline = time.monotonic() + 12
            ack_seen = False
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(
                    lost_ack_socket.recv(), timeout=max(0.1, deadline - time.monotonic())
                )
                event = json.loads(raw) if isinstance(raw, str) else {}
                if event.get("type") == "error":
                    raise RuntimeError(
                        f"station command failed before lost-ACK simulation: {event.get('message')}"
                    )
                if event.get("type") == "command.ack":
                    ack_seen = event.get("command_id") == lost_ack_command_id
                    break
            result["checks"]["lost_ack_command_ack_emitted"] = ack_seen
            if not ack_seen:
                raise RuntimeError("server did not emit command.ack for the DDS status command")
            result["checks"]["lost_ack_command_committed"] = await wait_for_command_checkpoint(
                lost_ack_command_id,
            )
            if not result["checks"]["lost_ack_command_committed"]:
                raise RuntimeError("station command ID did not reach the PostgreSQL checkpoint")
            # Do not consume any frame after send: the browser's pending
            # command remains unacknowledged when this transport is closed.
            await lost_ack_socket.close()
        result["lost_ack_command_id"] = lost_ack_command_id
        result["lost_ack_ack_dispatched_to_browser"] = False

        if args.failure_mode == "kill":
            fault_service = owner_service
            compose(args, "kill", fault_service)
            owner_stopped = True
        else:
            proxy_name = "db-proxy-a" if row.backend_node_id == "backend-a" else "db-proxy-b"
            fault_service = proxy_name
            async with AsyncExitStack() as channels:
                active_sockets = {}
                for channel, path, cookie in (
                    ("control", f"/ws/control/{session_id}", instructor_cookie),
                    ("call", f"/ws/call/{session_id}", trainee_cookie),
                    ("observe", f"/ws/observe/{session_id}", instructor_cookie),
                    ("station", f"/ws/station/{session_id}", trainee_cookie),
                ):
                    active_sockets[channel] = await channels.enter_async_context(
                        ws_connect(ws_base, path, cookie)
                    )
                compose(args, "stop", proxy_name)
                fault_stopped = True
                result["boundary_command_sent_to_old_owner"] = "arrived"
                running_services = compose(args, "ps", "--status", "running", "--services").splitlines()
                result["checks"]["owner_process_running"] = owner_service in running_services
                if owner_service not in running_services:
                    raise RuntimeError("owner process did not remain running during DB partition")
                closures = await asyncio.gather(*(
                    wait_for_fence(socket, timeout=args.takeover_timeout)
                    for socket in active_sockets.values()
                ))
                result["existing_channel_closures"] = dict(zip(active_sockets, closures))
                result["existing_channel_fence_notices"] = {
                    channel: closure["fence_notice"]
                    for channel, closure in result["existing_channel_closures"].items()
                }
                result["checks"].update({
                    f"existing_{channel}_closed": closure["closed"]
                    for channel, closure in result["existing_channel_closures"].items()
                })
                # Без БД старый владелец закрывает каналы fail-closed одним из двух
                # путей: auth не может сверить поколения учёток дольше 2 с — 1013,
                # supervisor lease замечает потерю владения (цикл 5 с) — 1012.
                # Auth обычно успевает первым; оба кода корректны, фронт их не
                # различает. Фактический код остаётся в existing_channel_closures.
                result["existing_channel_close_codes"] = {
                    channel: closure.get("close_code")
                    for channel, closure in result["existing_channel_closures"].items()
                }
                result["checks"].update({
                    f"existing_{channel}_closed_fail_closed": (
                        closure["fence_notice"] or closure.get("close_code") == 1013
                    )
                    for channel, closure in result["existing_channel_closures"].items()
                })

        deadline = time.monotonic() + args.takeover_timeout
        takeover = None
        while time.monotonic() < deadline:
            candidate = await session_row()
            if candidate and candidate.backend_node_id != row.backend_node_id:
                takeover = candidate
                break
            await asyncio.sleep(0.5)
        if takeover is None:
            raise RuntimeError("other backend did not take over the expired lease")
        result["takeover_owner"] = takeover.backend_node_id
        result["takeover_epoch"] = takeover.backend_fencing_epoch
        result["takeover_seconds"] = round(args.takeover_timeout - max(0, deadline - time.monotonic()), 2)
        result["checks"]["owner_changed"] = takeover.backend_node_id != row.backend_node_id
        result["checks"]["fencing_epoch_incremented"] = takeover.backend_fencing_epoch > old_epoch
        result["checks"]["checkpoint_restored"] = bool(takeover.live_state and takeover.checkpoint_at)

        result["checks"]["lost_ack_command_replayed"] = await replay_station_command(
            ws_base, session_id, trainee_cookie, lost_ack_command,
            lost_ack_command_id, attempts=args.reconnect_attempts,
        )

        if args.failure_mode == "partition":
            compose(args, "start", fault_service)
            fault_stopped = False
            await asyncio.sleep(6)  # let the surviving old process observe the newer epoch
            old_backend_port = args.backend_port if owner_service == "backend" else args.backend_b_port
            old_backend = f"http://127.0.0.1:{old_backend_port}"
            try:
                with urllib.request.urlopen(f"{old_backend}/api/health", timeout=5) as response:
                    result["checks"]["old_owner_process_healthy"] = response.status == 200
            except Exception as exc:
                result["checks"]["old_owner_process_healthy"] = False
                result["old_owner_health_error"] = f"{type(exc).__name__}: {exc}"
            stale = await probe_ws(
                old_backend.replace("http://", "ws://"),
                f"/ws/control/{session_id}", instructor_cookie,
            )
            result["stale_owner_control"] = stale
            result["checks"]["stale_owner_control_rejected"] = not stale["connected"]

        request = urllib.request.Request(
            f"{args.frontend_url}/api/sessions/{session_id}",
            headers={"Cookie": instructor_cookie},
        )
        rest_started = time.monotonic()
        rest_deadline = rest_started + args.rest_timeout
        rest_attempt = 0
        while time.monotonic() < rest_deadline:
            rest_attempt += 1
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=min(3.0, max(0.2, rest_deadline - time.monotonic())),
                    context=ssl._create_unverified_context(),
                ) as response:
                    result["checks"]["session_rest"] = response.status == 200
                if result["checks"]["session_rest"]:
                    result.pop("rest_error", None)
                    break
            except urllib.error.HTTPError as exc:
                result["rest_error"] = f"HTTP {exc.code}"
            except Exception as exc:
                result["rest_error"] = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(min(1, max(0, rest_deadline - time.monotonic())))
        result["checks"].setdefault("session_rest", False)
        result["rest_attempts"] = rest_attempt
        result["rest_elapsed_seconds"] = round(time.monotonic() - rest_started, 2)

        async def reconnect_probe(path: str, cookie: str) -> dict:
            last = {}
            for attempt in range(1, args.reconnect_attempts + 1):
                last = await probe_ws(ws_base, path, cookie)
                if last["connected"]:
                    last["attempts"] = attempt
                    return last
                await asyncio.sleep(1)
            last["attempts"] = args.reconnect_attempts
            return last

        probes = {
            "control": await reconnect_probe(f"/ws/control/{session_id}", instructor_cookie),
            "call": await reconnect_probe(f"/ws/call/{session_id}", trainee_cookie),
            "observe": await reconnect_probe(f"/ws/observe/{session_id}", instructor_cookie),
            "station": await reconnect_probe(f"/ws/station/{session_id}", trainee_cookie),
        }
        result["websockets"] = probes
        result["checks"].update({f"ws_{name}": probe["connected"] for name, probe in probes.items()})

        # Reconcile the race status against the recovered checkpoint. If it
        # committed before the fault, do not repeat it; otherwise issue it now.
        recovered_snapshot = await station_command(ws_base, session_id, trainee_cookie)
        recovered_status = recovered_snapshot["statuses"].get(service)
        result["boundary_status_after_takeover"] = recovered_status
        result["boundary_command_outcome"] = (
            "committed before takeover" if recovered_status == "arrived"
            else "not in recovered checkpoint; reconciled after takeover"
        )
        result["checks"]["boundary_command_reconciled"] = recovered_status in {
            "responding", "arrived",
        }
        result["dds_progress_after_takeover"] = []
        if recovered_status == "responding":
            recovered_snapshot = await station_command(ws_base, session_id, trainee_cookie, {
                "type": "card.status", "service": service, "status": "arrived",
                "comment": "Основание: доклад по карточке.\n"
                f"Сведения: бригада сообщила о прибытии; {service} notified.",
            })
            result["dds_progress_after_takeover"].append("arrived")
        # Continue the same card after owner recovery and require a stored final
        # score, not merely successful handshakes on the replacement owner.
        for status, detail in (
            ("working", "crew started work"),
            ("completed", "crew completed work"),
        ):
            snapshot = await station_command(ws_base, session_id, trainee_cookie, {
                "type": "card.status", "service": service, "status": status,
                "comment": f"Основание: доклад по карточке.\nСведения: {detail}; {service} notified.",
            })
            result["dds_progress_after_takeover"].append(status)
        async with websockets.connect(
            f"{ws_base}/ws/station/{session_id}",
            additional_headers={"Cookie": trainee_cookie},
            ssl=ssl._create_unverified_context() if ws_base.startswith("wss://") else None,
            open_timeout=10, ping_interval=None,
        ) as socket:
            await socket.send(json.dumps({"type": "station.finish"}))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                raw = await asyncio.wait_for(socket.recv(), timeout=10)
                event = json.loads(raw) if isinstance(raw, str) else {}
                if event.get("type") == "error":
                    raise RuntimeError(f"station finish rejected: {event.get('message')}")
                if event.get("type") == "score.ready":
                    result["checks"]["dds_finished"] = True
                    break
            else:
                raise RuntimeError("DDS exercise did not finish after takeover")
        report_request = urllib.request.Request(
            f"{args.frontend_url}/api/sessions/{session_id}/report",
            headers={"Cookie": instructor_cookie},
        )
        with urllib.request.urlopen(
            report_request, timeout=10, context=ssl._create_unverified_context(),
        ) as response:
            report = json.loads(response.read())
        result["dds_final_score"] = report.get("score_auto")
        card_results = report.get("card_results", [])
        if len(card_results) != 1:
            raise RuntimeError(f"expected one DDS card result, got {len(card_results)}")
        final_card = card_results[0]
        result["dds_card_metrics"] = {
            metric["key"]: metric["passed"] for metric in final_card["metrics"]
        }
        required_metric_keys = {
            "dds_ack", "dds_decision", "dds_crew", "dds_progress",
            "dds_completion", "dds_reply",
        }
        result["checks"]["dds_business_metrics_passed"] = all(
            result["dds_card_metrics"].get(key) is True for key in required_metric_keys
        )
        from collections import Counter

        observed_status_counts = Counter(
            action.get("status") for action in final_card["actions"]
            if action.get("type") == "card.status" and action.get("service") == service
        )
        expected_statuses = {"accepted", "responding", "arrived", "working", "completed"}
        result["checks"]["dds_statuses_persisted_exactly_once"] = all(
            observed_status_counts[status] == 1 for status in expected_statuses
        )
        result["dds_status_counts"] = dict(observed_status_counts)
        result["checks"]["lost_ack_status_recorded_once"] = (
            observed_status_counts["arrived"] == 1
        )
        passed = all(result["checks"].values())
        result["pass"] = passed
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if passed else 1
    finally:
        if instructor_cookie:
            try:
                await send_control(ws_base, session_id, instructor_cookie, {"type": "session.stop"})
            except Exception:
                pass
        if fault_stopped and fault_service:
            compose(args, "start", fault_service, check=False)
        if owner_stopped:
            compose(args, "start", owner_service, check=False)
        async with factory() as db:
            row = await db.scalar(select(Session).where(Session.id == session_id))
            if row:
                await db.execute(delete(Session).where(Session.id == session_id))
                await db.execute(delete(AuditLog).where(AuditLog.object_id == str(session_id)))
            await db.execute(delete(AuditLog).where(AuditLog.actor.in_([instructor_login, trainee_login])))
            await db.execute(delete(User).where(User.login.in_([instructor_login, trainee_login])))
            await db.execute(delete(Trainee).where(Trainee.name == trainee.name))
            await db.commit()
        await engine.dispose()
        await observer_engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--postgres-port", type=int, required=True)
    parser.add_argument("--backend-port", type=int, required=True)
    parser.add_argument("--backend-b-port", type=int, default=18001)
    parser.add_argument("--frontend-port", type=int, required=True)
    parser.add_argument("--tls-port", type=int, required=True)
    parser.add_argument("--postgres-password", required=True)
    parser.add_argument("--session-secret", required=True)
    parser.add_argument("--tls-cert-dir", required=True)
    parser.add_argument("--frontend-url", required=True)
    parser.add_argument("--scenario", default="fire-apartment-l2")
    parser.add_argument("--takeover-timeout", type=float, default=30)
    parser.add_argument("--reconnect-attempts", type=int, default=5)
    parser.add_argument("--rest-timeout", type=float, default=60)
    parser.add_argument("--failure-mode", choices=("kill", "partition"), default="kill")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
