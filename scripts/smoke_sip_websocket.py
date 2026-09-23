#!/usr/bin/env python3
"""Проверка Digest REGISTER браузерного SIP-транспорта Asterisk по WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import websockets

from smoke_sip import CRLF, compose_password, digest_header, parse_message


def local_ws(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {
        "127.0.0.1", "localhost", "::1"
    }:
        raise ValueError("WebSocket URL должен вести на локальный стенд")
    return value


def register_message(
    host: str, user: str, call_id: str, cseq: int, branch: str,
    authorization: str | None = None, expires: int = 300,
) -> str:
    uri = f"sip:{host}"
    tag = call_id[:10]
    lines = [
        f"REGISTER {uri} SIP/2.0",
        f"Via: SIP/2.0/WSS browser.invalid;branch={branch};rport",
        "Max-Forwards: 70",
        f"From: <sip:{user}@{host}>;tag={tag}",
        f"To: <sip:{user}@{host}>",
        f"Call-ID: {call_id}@lct-websocket-smoke",
        f"CSeq: {cseq} REGISTER",
        f"Contact: <sip:{user}@browser.invalid;transport=ws>;expires={expires}",
        f"Expires: {expires}",
        "User-Agent: LCT-WebSocket-smoke/1.0",
    ]
    if authorization:
        lines.append(f"Authorization: {authorization}")
    lines.extend(["Content-Length: 0", "", ""])
    return CRLF.join(lines)


async def receive_response(socket, timeout: float = 5) -> tuple[str, dict[str, str], str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = await asyncio.wait_for(socket.recv(), timeout=deadline - time.monotonic())
        parsed = parse_message(raw.encode() if isinstance(raw, str) else raw)
        if parsed[0].startswith("SIP/2.0"):
            return parsed
    raise TimeoutError("нет ответа SIP по WebSocket")


async def run(args: argparse.Namespace) -> int:
    url = local_ws(args.url)
    password = args.password or compose_password(args.user)
    host = args.domain
    call_id = uuid.uuid4().hex
    registrar = f"sip:{host}"
    started = time.perf_counter()
    tls = None
    if url.startswith("wss:"):
        tls = ssl._create_unverified_context() if args.insecure else True
    async with websockets.connect(
        url,
        subprotocols=["sip"],
        open_timeout=args.timeout,
        close_timeout=2,
        ping_interval=None,
        ssl=tls,
    ) as socket:
        await socket.send(register_message(
            host, args.user, call_id, 1, f"z9hG4bK{uuid.uuid4().hex}"
        ))
        status, headers, _ = await receive_response(socket, args.timeout)
        if "401" not in status:
            raise RuntimeError(f"ожидался Digest challenge, получено {status}")
        challenge = headers.get("www-authenticate")
        if not challenge:
            raise RuntimeError("в challenge отсутствует WWW-Authenticate")
        auth = digest_header(challenge, args.user, password, "REGISTER", registrar)
        await socket.send(register_message(
            host, args.user, call_id, 2, f"z9hG4bK{uuid.uuid4().hex}", auth
        ))
        status, _, _ = await receive_response(socket, args.timeout)
        if "200" not in status:
            raise RuntimeError(f"WebSocket REGISTER отклонён: {status}")
        elapsed_ms = (time.perf_counter() - started) * 1000
        # Снимаем регистрацию тем же аутентифицированным диалогом, чтобы smoke
        # не оставлял контакт и не мешал браузерному абоненту.
        await socket.send(register_message(
            host, args.user, call_id, 3, f"z9hG4bK{uuid.uuid4().hex}", auth, expires=0
        ))
        unregister_status, _, _ = await receive_response(socket, args.timeout)

    result = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "url": url,
        "extension": args.user,
        "websocket_subprotocol": "sip",
        "digest_challenge": "ok",
        "registration": "ok",
        "unregister": "ok" if "200" in unregister_status else unregister_status,
        "registration_ms": round(elapsed_ms, 3),
        "passed": "200" in unregister_status,
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8088/ws")
    parser.add_argument("--domain", default="localhost")
    parser.add_argument("--user", default="6101")
    parser.add_argument("--password")
    parser.add_argument("--timeout", type=float, default=8)
    parser.add_argument("--insecure", action="store_true",
                        help="доверять только self-signed сертификату локального стенда")
    parser.add_argument("--output", type=Path)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
