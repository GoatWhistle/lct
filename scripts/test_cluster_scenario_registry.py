#!/usr/bin/env python3
"""Cross-process smoke: publish a trainee scenario on backend A, start it on B.

Use only with a disposable local Compose project and its own PostgreSQL. The
smoke creates temporary accounts, group, proposal, and DDS session and removes
their rows in ``finally``. It never writes evidence into the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from uuid import UUID, uuid4

import websockets


def compose(args: argparse.Namespace, *command: str) -> str:
    env = os.environ.copy()
    env.update({
        "POSTGRES_PORT": str(args.postgres_port),
        "BACKEND_PORT": str(args.backend_a_port),
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
    command_result = subprocess.run(
        ["docker", "compose", "-p", args.project,
         *[part for file in files for part in ("-f", file)], *command],
        check=False, capture_output=True, text=True, env=env,
    )
    if command_result.returncode:
        raise RuntimeError(
            f"docker compose {' '.join(command)} failed: {command_result.stderr.strip()}"
        )
    return command_result.stdout


def request_json(base: str, path: str, *, cookie: str | None = None,
                 payload: dict | None = None) -> dict | list:
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(f"{base}{path}", data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def login(base: str, login_name: str, password: str) -> str:
    payload = json.dumps({"login": login_name, "password": password}).encode()
    request = urllib.request.Request(
        f"{base}/api/auth/login", data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        cookie = response.headers.get("Set-Cookie", "").split(";", 1)[0]
    if not cookie.startswith("lct_session="):
        raise RuntimeError(f"{base} did not issue the session cookie")
    return cookie


async def main(args: argparse.Namespace) -> int:
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.auth import hash_password
    from app.db.models import (
        AuditLog, Group, Scenario as ScenarioRow, ScenarioSubmission,
        Session, Trainee, User,
    )
    from app.domain import ekp
    from app.scenarios import store

    db_url = (f"postgresql+asyncpg://lct:{args.postgres_password}@127.0.0.1:"
              f"{args.postgres_port}/lct")
    engine = create_async_engine(db_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suffix = uuid4().hex[:12]
    teacher_login, trainee_login = f"registry-i-{suffix}", f"registry-t-{suffix}"
    teacher_password, trainee_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    teacher_name, trainee_name = "Registry smoke instructor", f"Registry smoke {suffix}"
    group = Group(name=f"registry-smoke-{suffix}", owner_login=teacher_login)
    trainee = Trainee(name=trainee_name)
    scenario_id: str | None = None
    session_id = uuid4()
    session_ids: list[UUID] = []
    submission_id: UUID | None = None
    teacher_cookie = trainee_cookie = None
    session_ws_base = None
    ssl_context = None
    result: dict = {"checks": {}}
    a = f"http://127.0.0.1:{args.backend_a_port}"
    b = f"http://127.0.0.1:{args.backend_b_port}"
    try:
        store.load_from_disk(Path(args.scenarios))
        source = store.get(args.source_scenario)
        if source is None or source.ground_truth.dds is None:
            raise RuntimeError(f"invalid source scenario {args.source_scenario}")
        trainee.group_id = None
        async with factory() as db:
            db.add(group)
            await db.flush()
            trainee.group_id = group.id
            db.add(trainee)
            await db.flush()
            db.add_all([
                User(login=teacher_login, full_name=teacher_name,
                     password_hash=hash_password(teacher_password), role="instructor",
                     blocked=False),
                User(login=trainee_login, full_name=trainee_name,
                     password_hash=hash_password(trainee_password), role="trainee",
                     trainee_id=trainee.id, blocked=False),
            ])
            await db.commit()
        result["checks"]["backend_a_healthy"] = request_json(a, "/api/health").get("status") == "ok"
        result["checks"]["backend_b_healthy"] = request_json(b, "/api/health").get("status") == "ok"
        teacher_cookie = login(a, teacher_login, teacher_password)
        trainee_cookie = login(a, trainee_login, trainee_password)

        incident_group = ekp.incident(source.ground_truth.incident_code).group
        created = request_json(a, "/api/scenario-submissions", cookie=trainee_cookie, payload={
            "title": f"Межузловая проверка {suffix}", "level": source.level.value,
            "kio": {
                "caller_name": "Тестовый заявитель",
                "caller_contact": "+7 900 000-00-00",
                "address": f"Москва, учебная улица, дом {suffix[:3]}",
                "description": "На учебном объекте обнаружено задымление и нужна бригада.",
                "incident_group": incident_group,
                "signs": source.signs,
                "incident_type": source.type.value,
                "dds": source.ground_truth.dds.value,
                "victims_count": 0,
                "fire": {"object_kind": "учебное помещение", "fire_nature": "задымление"},
            },
        })
        submission_id = UUID(created["id"])
        approved = request_json(a, f"/api/scenario-submissions/{submission_id}/review",
                                cookie=teacher_cookie,
                                payload={"decision": "approve", "comment": "Межузловой smoke."})
        scenario_id = approved["scenario_id"]
        result["scenario_id"] = scenario_id
        result["published_on"] = "backend-a"
        result["checks"]["submission_approved_on_a"] = approved["status"] == "approved"

        # WS start is the first scenario operation on the selected target node.
        # With --frontend-url, UUIDs are tried until Nginx consistent-hash routes
        # one to B; every candidate is pinned for all session channels.
        session_ws_base = args.frontend_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        ) if args.frontend_url else b.replace("http://", "ws://")
        ssl_context = ssl._create_unverified_context() if session_ws_base.startswith("wss://") else None
        target_backend = "backend-b"
        max_route_attempts = 12 if args.frontend_url else 1
        session_row = None
        for route_attempt in range(max_route_attempts):
            candidate_id = uuid4()
            session_ids.append(candidate_id)
            session_id = candidate_id
            ws_url = f"{session_ws_base}/ws/control/{session_id}"
            async with websockets.connect(
                ws_url, additional_headers={"Cookie": teacher_cookie}, ssl=ssl_context,
                open_timeout=15, ping_interval=None,
            ) as control:
                await control.send(json.dumps({
                    "type": "scenario.start", "scenario_id": scenario_id,
                    "scenario_ids": [scenario_id], "trainee": trainee_name,
                    "trainee_id": str(trainee.id), "dds_service": created["kio"]["notify"][0],
                    "mode": "training", "exercise": "dds",
                }, ensure_ascii=False))
                async with factory() as db:
                    deadline = time.monotonic() + 8
                    session_row = None
                    while time.monotonic() < deadline:
                        session_row = await db.scalar(
                            select(Session).where(Session.id == candidate_id)
                        )
                        if session_row and session_row.backend_node_id:
                            break
                        await asyncio.sleep(0.1)
                if session_row and session_row.backend_node_id == target_backend:
                    break
                if session_row and session_row.backend_node_id:
                    result.setdefault("nginx_route_attempts", []).append({
                        "session_id": str(candidate_id),
                        "owner": session_row.backend_node_id,
                    })
                    await control.send(json.dumps({"type": "session.stop"}))
                    await asyncio.sleep(0.15)
                else:
                    raise RuntimeError("session.start was not persisted through the selected route")
        result["checks"]["session_started_on_backend_b"] = bool(
            session_row and session_row.backend_node_id == target_backend
            and session_row.scenario_id == scenario_id and session_row.live_state
        )
        if not result["checks"]["session_started_on_backend_b"]:
            raise RuntimeError("could not route a fresh session to backend B")
        result["session_id"] = str(session_id)
        result["route_attempts"] = len(session_ids)

        # Prove the user-visible catalog/detail routes while B is still alive.
        catalog = request_json(b, "/api/scenarios", cookie=teacher_cookie)
        entry = next((item for item in catalog if item["id"] == scenario_id), None)
        result["checks"]["visible_in_backend_b_catalog"] = entry is not None
        result["checks"]["owned_by_teacher_on_backend_b"] = bool(entry and entry["can_manage"])
        detail = request_json(b, f"/api/scenarios/{scenario_id}", cookie=teacher_cookie)
        result["checks"]["detail_loaded_from_backend_b"] = (
            detail.get("student_card", {}).get("address")
            == created["kio"]["address"]
        )

        if args.failure_mode == "kill-backend-b":
            old_epoch = session_row.backend_fencing_epoch
            compose(args, "kill", "backend-b")
            result["checks"]["backend_b_killed_after_start"] = True
            deadline = time.monotonic() + args.takeover_timeout
            takeover_row = None
            while time.monotonic() < deadline:
                async with factory() as db:
                    takeover_row = await db.scalar(
                        select(Session).where(Session.id == session_id)
                    )
                if takeover_row and takeover_row.backend_node_id == "backend-a":
                    break
                await asyncio.sleep(0.25)
            result["checks"]["takeover_to_backend_a"] = bool(
                takeover_row and takeover_row.backend_node_id == "backend-a"
            )
            result["checks"]["fencing_epoch_incremented"] = bool(
                takeover_row and takeover_row.backend_fencing_epoch > old_epoch
            )
            result["checks"]["checkpoint_restored_after_kill"] = bool(
                takeover_row and takeover_row.live_state and takeover_row.checkpoint_at
            )
            if not all(result["checks"][key] for key in (
                "takeover_to_backend_a", "fencing_epoch_incremented",
                "checkpoint_restored_after_kill",
            )):
                raise RuntimeError("backend A did not take over the killed B session")
            result["takeover_seconds"] = round(
                args.takeover_timeout - max(0, deadline - time.monotonic()), 2
            )

        station_url = f"{session_ws_base}/ws/station/{session_id}"
        station_owner_key = (
            "dds_card_received_after_takeover" if args.failure_mode == "kill-backend-b"
            else "dds_card_received_on_backend_b"
        )
        station_snapshot = None
        received_card = None
        last_station_error = None
        for reconnect_attempt in range(8):
            try:
                async with websockets.connect(
                    station_url, additional_headers={"Cookie": trainee_cookie}, ssl=ssl_context,
                    open_timeout=10, ping_interval=None,
                ) as station:
                    for _ in range(60):
                        event = json.loads(await asyncio.wait_for(station.recv(), timeout=10))
                        if event.get("type") == "error":
                            raise RuntimeError(
                                f"station error after route/failover: {event.get('message')}"
                            )
                        if event.get("type") == "card.received":
                            received_card = event["card"]
                        elif event.get("type") == "station.state":
                            station_snapshot = event.get("snapshot")
                        if received_card is not None and station_snapshot is not None:
                            break
                    if received_card is None or station_snapshot is None:
                        raise RuntimeError("station reconnect did not restore card and snapshot")
                    break
            except (TimeoutError, OSError, websockets.exceptions.WebSocketException,
                    RuntimeError) as exc:
                last_station_error = exc
                if reconnect_attempt == 7:
                    raise RuntimeError(f"station did not recover through proxy: {exc}") from exc
                await asyncio.sleep(1)
        result["checks"][station_owner_key] = received_card is not None
        result["checks"]["approved_kio_preserved"] = bool(
            received_card
            and received_card.get("address") == created["kio"]["address"]
            and received_card.get("caller_name") == created["kio"]["caller_name"]
        )
        if args.failure_mode == "kill-backend-b":
            command_id = str(uuid4())
            command = {
                "type": "card.status", "service": station_snapshot["managed_service"],
                "status": "accepted",
                "comment": (
                    "Основание: доклад по карточке.\n"
                    f"Сведения: карточка повторно принята после takeover; {station_snapshot['managed_service']} notified."
                ),
                "_command_id": command_id,
            }
            async with websockets.connect(
                station_url, additional_headers={"Cookie": trainee_cookie}, ssl=ssl_context,
                open_timeout=10, ping_interval=None,
            ) as station:
                await station.send(json.dumps(command, ensure_ascii=False))
                acked = False
                for _ in range(40):
                    event = json.loads(await asyncio.wait_for(station.recv(), timeout=10))
                    if event.get("type") == "error":
                        raise RuntimeError(f"station command failed after takeover: {event.get('message')}")
                    if event.get("type") == "command.ack" and event.get("command_id") == command_id:
                        acked = True
                        break
                result["checks"]["station_command_acked_after_takeover"] = acked
            async with factory() as db:
                restored = await db.scalar(select(Session).where(Session.id == session_id))
                result["checks"]["post_takeover_command_checkpointed"] = bool(
                    restored and restored.backend_node_id == "backend-a"
                    and command_id in (restored.live_state or {}).get(
                        "processed_station_commands", []
                    )
                )
        result["pass"] = all(result["checks"].values())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["pass"] else 1
    finally:
        if args.failure_mode == "kill-backend-b" and args.project:
            try:
                compose(args, "start", "backend-b")
            except Exception:
                pass
        if teacher_cookie and session_ws_base:
            try:
                stop_base = (
                    f"ws://127.0.0.1:{args.backend_a_port}"
                    if args.failure_mode == "kill-backend-b" else session_ws_base
                )
                stop_ssl = None if stop_base.startswith("ws://") else ssl_context
                async with websockets.connect(
                    f"{stop_base}/ws/control/{session_id}",
                    additional_headers={"Cookie": teacher_cookie}, ssl=stop_ssl,
                    open_timeout=5, ping_interval=None,
                ) as control:
                    await control.send(json.dumps({"type": "session.stop"}))
            except Exception:
                pass
        async with factory() as db:
            await db.execute(delete(AuditLog).where(
                AuditLog.object_id.in_([str(item) for item in session_ids])
            ))
            if submission_id:
                await db.execute(delete(ScenarioSubmission).where(
                    ScenarioSubmission.id == submission_id
                ))
            if session_ids:
                await db.execute(delete(Session).where(Session.id.in_(session_ids)))
            if scenario_id:
                await db.execute(delete(ScenarioRow).where(ScenarioRow.id == scenario_id))
            await db.execute(delete(AuditLog).where(
                AuditLog.actor.in_([teacher_login, trainee_login])
            ))
            await db.execute(delete(User).where(User.login.in_([teacher_login, trainee_login])))
            await db.execute(delete(Trainee).where(Trainee.name == trainee_name))
            await db.execute(delete(Group).where(Group.id == group.id))
            await db.commit()
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-port", type=int, required=True)
    parser.add_argument("--backend-a-port", type=int, required=True)
    parser.add_argument("--backend-b-port", type=int, required=True)
    parser.add_argument("--postgres-password", required=True)
    parser.add_argument("--frontend-url", help="optional TLS Nginx URL; tests consistent-hash routing")
    parser.add_argument("--failure-mode", choices=("none", "kill-backend-b"), default="none")
    parser.add_argument("--project", help="Compose project, required for --failure-mode kill-backend-b")
    parser.add_argument("--tls-cert-dir", default="")
    parser.add_argument("--session-secret", default="")
    parser.add_argument("--frontend-port", type=int, default=15180)
    parser.add_argument("--tls-port", type=int, default=15443)
    parser.add_argument("--takeover-timeout", type=float, default=45)
    parser.add_argument("--source-scenario", default="t01-1-fire-container")
    parser.add_argument("--scenarios", default="scenarios")
    parsed = parser.parse_args()
    if parsed.failure_mode == "kill-backend-b" and not (
        parsed.frontend_url and parsed.project and parsed.session_secret and parsed.tls_cert_dir
    ):
        parser.error("kill-backend-b requires --frontend-url, --project, --session-secret and --tls-cert-dir")
    raise SystemExit(asyncio.run(main(parsed)))
