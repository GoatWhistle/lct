#!/usr/bin/env python3
"""Local-only WebSocket load probe for 20 DDS lessons / 100 clients.

The probe starts real DDS exercises, holds one station and N observers per
lesson, and times valid ``zone.decision`` -> ``station.state`` round trips.
It deliberately does not pretend to measure browser paint, voice or DB TPS.
"""

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from contextlib import AsyncExitStack
from uuid import uuid4

import websockets


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil(percent / 100 * len(ordered)) - 1
    return ordered[max(0, rank)]


def cookie(base: str, dev_token: bool) -> str:
    endpoint = "/api/auth/dev-token" if dev_token else "/api/auth/login"
    if dev_token:
        data = b""
    else:
        login = os.getenv("LCT_LOAD_LOGIN")
        password = os.getenv("LCT_LOAD_PASSWORD")
        if not login or not password:
            raise RuntimeError("set LCT_LOAD_LOGIN and LCT_LOAD_PASSWORD (or use --dev-token)")
        data = json.dumps({"login": login, "password": password}).encode()
    request = urllib.request.Request(
        base + endpoint, data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            session_cookie = response.headers.get("Set-Cookie", "").split(";", 1)[0]
            if not session_cookie.startswith("lct_session="):
                raise RuntimeError("login did not return lct_session cookie")
            return session_cookie
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"auth HTTP {exc.code}; dev-token requires DEV_AUTH_BYPASS=true") from exc


async def read_type(ws, expected: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"waited for {expected}")
        message = json.loads(await asyncio.wait_for(ws.recv(), remaining))
        if message.get("type") == "error":
            raise RuntimeError(f"server error: {message}")
        if message.get("type") == expected:
            return message


async def observer(ws, stop: asyncio.Event, counts: Counter, errors: list[str]) -> None:
    while not stop.is_set():
        try:
            message = json.loads(await asyncio.wait_for(ws.recv(), 2))
            counts[message.get("type", "unknown")] += 1
            if message.get("type") == "error":
                errors.append(str(message))
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # failure must be reported, not swallowed
            if not stop.is_set():
                errors.append(f"observer: {type(exc).__name__}: {exc}")
            return


async def main(args: argparse.Namespace) -> int:
    parsed = urllib.parse.urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("load probe only accepts a local http://localhost/127.0.0.1/::1 backend")
    if args.clients < args.sessions or args.sessions < 1 or args.rounds < 1:
        raise ValueError("require clients >= sessions >= 1 and rounds >= 1")
    if args.clients % args.sessions:
        raise ValueError("clients must be divisible by sessions")
    if args.clients > 500 or args.sessions > 100:
        raise ValueError("safety cap: at most 500 clients and 100 sessions")

    base = args.base_url.rstrip("/")
    ws_base = "ws" + base.removeprefix("http")
    headers = {"Cookie": cookie(base, args.dev_token)}
    ids = [str(uuid4()) for _ in range(args.sessions)]
    errors: list[str] = []
    counts: Counter = Counter()
    times: list[float] = []
    stop = asyncio.Event()
    observer_tasks: list[asyncio.Task] = []
    start = time.monotonic()
    started_ids: set[str] = set()
    try:
        async with AsyncExitStack() as stack:
            # Control connections are setup-only; they are not counted as clients.
            async def start_lesson(index: int, session_id: str) -> None:
                async with websockets.connect(
                    f"{ws_base}/ws/control/{session_id}", additional_headers=headers,
                    open_timeout=args.timeout, ping_interval=None,
                ) as ws:
                    await ws.send(json.dumps({
                        "type": "scenario.start", "scenario_id": args.scenario,
                        "trainee": f"load-{index:03d}", "mode": "training", "exercise": "dds",
                    }))
                started_ids.add(session_id)

            await asyncio.gather(*(start_lesson(i, sid) for i, sid in enumerate(ids)))

            # Observe proves every lesson actually started, not just WS accepted.
            async def connect_session(session_id: str):
                deadline = time.monotonic() + args.timeout
                while True:
                    station = await stack.enter_async_context(websockets.connect(
                        f"{ws_base}/ws/station/{session_id}", additional_headers=headers,
                        open_timeout=args.timeout, ping_interval=None,
                    ))
                    try:
                        card = await read_type(station, "card.received", args.timeout)
                        break
                    except RuntimeError as exc:
                        if "session_not_found" not in str(exc) or time.monotonic() >= deadline:
                            raise
                        await asyncio.sleep(0.1)
                if not card.get("card"):
                    raise RuntimeError(f"empty card in {session_id}")
                await read_type(station, "station.state", args.timeout)
                for _ in range(args.clients // args.sessions - 1):
                    ws = await stack.enter_async_context(websockets.connect(
                        f"{ws_base}/ws/observe/{session_id}", additional_headers=headers,
                        open_timeout=args.timeout, ping_interval=None,
                    ))
                    await read_type(ws, "session.snapshot", args.timeout)
                    observer_tasks.append(asyncio.create_task(observer(ws, stop, counts, errors)))
                return station

            stations = await asyncio.gather(*(connect_session(sid) for sid in ids))
            ready_at = time.monotonic()
            # Each station serializes its own commands; all sessions run together.
            async def exercise(station) -> None:
                for round_number in range(args.rounds):
                    sent_at = time.monotonic()
                    await station.send(json.dumps({
                        "type": "zone.decision", "in_zone": round_number % 2 == 0,
                    }))
                    await read_type(station, "station.state", args.timeout)
                    times.append((time.monotonic() - sent_at) * 1000)

            await asyncio.gather(*(exercise(ws) for ws in stations))
            # Let fan-out queues flush, then record observer delivery count.
            await asyncio.sleep(0.25)
            load_seconds = time.monotonic() - ready_at
            stop.set()
            for task in observer_tasks:
                task.cancel()
            await asyncio.gather(*observer_tasks, return_exceptions=True)

            result = {
                "target": base, "scenario": args.scenario,
                "sessions_started": len(ids), "clients_connected": args.clients,
                "stations": len(stations), "observers": len(observer_tasks),
                "rounds_per_session": args.rounds, "actions_completed": len(times),
                "observer_state_events": counts["session.snapshot"],
                "observer_state_expected": args.rounds * len(observer_tasks),
                "round_trip_ms": {
                    "min": min(times), "median": statistics.median(times),
                    "p95": percentile(times, 95), "max": max(times),
                },
                "actions_per_second": len(times) / load_seconds,
                "setup_seconds": ready_at - start, "load_seconds": load_seconds,
                "errors": errors,
                "pass_backend_event_2s": percentile(times, 95) <= 2000 and not errors
                and counts["session.snapshot"] >= args.rounds * len(observer_tasks),
                "scope": "backend WS event RTT and fan-out only; not browser UI, VoIP, DB TPS or 100 distinct accounts",
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["pass_backend_event_2s"] else 1
    finally:
        stop.set()
        for task in observer_tasks:
            task.cancel()
        await asyncio.gather(*observer_tasks, return_exceptions=True)
        # End only lessons opened by this invocation, including on failure.
        async def end_lesson(session_id: str) -> None:
            try:
                async with websockets.connect(
                    f"{ws_base}/ws/control/{session_id}", additional_headers=headers,
                    open_timeout=args.timeout,
                ) as control:
                    await control.send('{"type":"session.stop"}')
                    await asyncio.sleep(0.05)
            except Exception as exc:
                print(f"cleanup warning {session_id}: {exc}", file=sys.stderr)

        await asyncio.gather(*(end_lesson(sid) for sid in started_ids))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--clients", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--scenario", default="fire-apartment-l2")
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--dev-token", action="store_true", help="local dev stand only")
    try:
        sys.exit(asyncio.run(main(parser.parse_args())))
    except Exception as exc:  # network/protocol failures are setup errors, not measured failures
        print(f"load probe failed: {exc}", file=sys.stderr)
        sys.exit(2)
