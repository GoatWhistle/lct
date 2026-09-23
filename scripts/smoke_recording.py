#!/usr/bin/env python3
"""Живая проверка WAV-записи на локальном backend без микрофона."""

import argparse
import asyncio
import io
import inspect
import json
import math
import struct
import urllib.error
import urllib.request
import wave
from uuid import uuid4

import websockets

from load_browser import control, local_url, login_cookie


async def receive_type(socket, event_type: str, limit: int = 30) -> dict:
    for _ in range(limit):
        message = await asyncio.wait_for(socket.recv(), timeout=10)
        if isinstance(message, str):
            payload = json.loads(message)
            if payload.get("type") == event_type:
                return payload
    raise RuntimeError(f"событие {event_type} не получено")


async def run(args: argparse.Namespace) -> int:
    backend = local_url(args.backend_url, {"http"})
    cookie = login_cookie(backend, args.login, args.password)
    ws_base = "ws" + backend.removeprefix("http")
    session_id = str(uuid4())
    await control(ws_base, session_id, cookie, {
        "type": "scenario.start",
        "scenario_id": args.scenario,
        "trainee": "recording-smoke",
        "mode": "training",
        "exercise": "call",
    })
    header_name = "additional_headers" if "additional_headers" in inspect.signature(
        websockets.connect
    ).parameters else "extra_headers"
    async with websockets.connect(
        f"{ws_base}/ws/call/{session_id}",
        **{header_name: {"Cookie": cookie}},
        open_timeout=10,
        ping_interval=None,
    ) as socket:
        await receive_type(socket, "call.incoming")
        await socket.send(json.dumps({"type": "call.answer"}))
        await receive_type(socket, "call.started")
        for frame_index in range(50):
            frame = b"".join(
                struct.pack("<h", round(5000 * math.sin(2 * math.pi * 440 * (frame_index * 320 + i) / 16000)))
                for i in range(320)
            )
            await socket.send(frame)
            await asyncio.sleep(0.02)
        await socket.send(json.dumps({"type": "call.hangup"}))
        await receive_type(socket, "call.ended")

    request = urllib.request.Request(
        f"{backend}/api/sessions/{session_id}/recording.wav",
        headers={"Cookie": cookie},
    )
    for _ in range(20):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                content_type = response.headers.get_content_type()
                payload = response.read()
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 409}:
                raise
            await asyncio.sleep(0.1)
    else:
        raise RuntimeError("готовая WAV-запись не появилась")

    with wave.open(io.BytesIO(payload), "rb") as source:
        channels = source.getnchannels()
        sample_rate = source.getframerate()
        frames = source.getnframes()
        duration = frames / sample_rate
    result = {
        "backend": backend,
        "session_id": session_id,
        "content_type": content_type,
        "bytes": len(payload),
        "channels": channels,
        "sample_rate": sample_rate,
        "frames": frames,
        "duration_seconds": duration,
        "pass_wav_recording": (
            content_type == "audio/wav" and channels == 1 and sample_rate == 16_000
            and duration >= 0.9 and payload.startswith(b"RIFF")
        ),
        "scope": "synthetic 440-Hz operator audio; VOICE_ENABLED=false, therefore no TTS track",
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(rendered + "\n")
    return 0 if result["pass_wav_recording"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--login", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--scenario", default="fire-apartment-l2")
    parser.add_argument("--output")
    try:
        raise SystemExit(asyncio.run(run(parser.parse_args())))
    except Exception as exc:
        print(f"recording smoke failed: {type(exc).__name__}: {exc}")
        raise SystemExit(2)
