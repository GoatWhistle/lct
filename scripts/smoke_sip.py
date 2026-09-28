#!/usr/bin/env python3
"""Local SIP/RTP acceptance check for the bundled Asterisk service.

The check registers extension 6001, calls the Echo application at 7000 and
measures round-trip time for PCMU RTP packets.  It intentionally uses only the
Python standard library so the offline delivery does not need a SIP test tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import socket
import statistics
import struct
import subprocess
import time
import uuid
from pathlib import Path


CRLF = "\r\n"


def md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()  # noqa: S324 - SIP Digest requires MD5


def parse_message(data: bytes) -> tuple[str, dict[str, str], str]:
    text = data.decode("utf-8", "replace")
    head, _, body = text.partition(CRLF + CRLF)
    lines = head.split(CRLF)
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return lines[0], headers, body


def digest_header(challenge: str, user: str, password: str, method: str, uri: str) -> str:
    parsed = {key: quoted or plain for key, quoted, plain in re.findall(
        r'(\w+)=(?:"([^"]*)"|([^,\s]+))', challenge
    )}
    realm = parsed["realm"]
    nonce = parsed["nonce"]
    response_parts = [md5(f"{user}:{realm}:{password}"), nonce]
    attrs = [
        f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"',
        f'uri="{uri}"',
    ]
    qop = parsed.get("qop", "").split(",")[0]
    if qop:
        nc = "00000001"
        cnonce = uuid.uuid4().hex[:16]
        response_parts.extend([nc, cnonce, qop])
        attrs.extend([f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"'])
    response_parts.append(md5(f"{method}:{uri}"))
    attrs.extend([f'response="{md5(":".join(response_parts))}"', "algorithm=MD5"])
    return "Digest " + ", ".join(attrs)


def request(
    method: str,
    uri: str,
    host: str,
    local_port: int,
    user: str,
    call_id: str,
    cseq: int,
    branch: str,
    *,
    authorization: str | None = None,
    body: str = "",
    to_user: str | None = None,
    from_tag: str | None = None,
    to_header: str | None = None,
) -> bytes:
    target = to_user or user
    from_tag = from_tag or uuid.uuid4().hex[:10]
    headers = [
        f"{method} {uri} SIP/2.0",
        f"Via: SIP/2.0/UDP 127.0.0.1:{local_port};branch={branch};rport",
        "Max-Forwards: 70",
        f'From: <sip:{user}@{host}>;tag={from_tag}',
        f"To: {to_header or f'<sip:{target}@{host}>'}",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} {method}",
        f"Contact: <sip:{user}@127.0.0.1:{local_port};transport=udp>",
        "User-Agent: LCT-offline-smoke/1.0",
    ]
    if authorization:
        headers.append(f"Authorization: {authorization}")
    if body:
        headers.append("Content-Type: application/sdp")
    headers.extend([f"Content-Length: {len(body.encode())}", "", body])
    return CRLF.join(headers).encode()


def receive_final(sock: socket.socket, timeout: float = 3.0) -> tuple[str, dict[str, str], str]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        sock.settimeout(max(0.05, deadline - time.monotonic()))
        parsed = parse_message(sock.recvfrom(65535)[0])
        last = parsed
        status = parsed[0]
        if status.startswith("SIP/2.0") and not status.startswith("SIP/2.0 1"):
            return parsed
    raise TimeoutError(f"No final SIP response; last={last and last[0]}")


def compose_password(user: str) -> str:
    result = subprocess.run(
        [
            "docker", "compose", "exec", "-T", "sip", "cat", "/var/lib/lct-sip/credentials.env",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    prefix = f"SIP_{user}_PASSWORD="
    for line in result.stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    raise RuntimeError(f"No password for SIP extension {user}")


def send_ack(
    sock: socket.socket, host: str, port: int, user: str, call_id: str, cseq: int,
    target: str, from_tag: str, to_header: str | None = None,
) -> None:
    payload = request(
        "ACK", f"sip:{target}@{host}", host, sock.getsockname()[1], user, call_id, cseq,
        f"z9hG4bK{uuid.uuid4().hex}", to_user=target, from_tag=from_tag,
        to_header=to_header,
    )
    sock.sendto(payload, (host, port))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5060)
    parser.add_argument("--rtp-host-offset", type=int, default=0,
                        help="Host-to-container RTP port offset (1000 for 11000:10000 mappings)")
    parser.add_argument("--user", default="6001")
    parser.add_argument("--password", default=os.getenv("SIP_PASSWORD"))
    parser.add_argument("--packets", type=int, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    password = args.password or compose_password(args.user)

    sip = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sip.bind(("127.0.0.1", 0))
    sip_port = sip.getsockname()[1]
    registrar = f"sip:{args.host}"

    register_id = f"{uuid.uuid4().hex}@lct-smoke"
    packet = request("REGISTER", registrar, args.host, sip_port, args.user, register_id, 1,
                     f"z9hG4bK{uuid.uuid4().hex}")
    sip.sendto(packet, (args.host, args.port))
    status, headers, _ = receive_final(sip)
    if "401" not in status:
        raise RuntimeError(f"Expected REGISTER challenge, got {status}")
    challenge = headers.get("www-authenticate")
    if not challenge:
        raise RuntimeError("REGISTER challenge has no WWW-Authenticate header")
    auth = digest_header(challenge, args.user, password, "REGISTER", registrar)
    packet = request("REGISTER", registrar, args.host, sip_port, args.user, register_id, 2,
                     f"z9hG4bK{uuid.uuid4().hex}", authorization=auth)
    sip.sendto(packet, (args.host, args.port))
    status, _, _ = receive_final(sip)
    if "200" not in status:
        raise RuntimeError(f"REGISTER failed: {status}")

    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp.bind(("0.0.0.0", 0))
    rtp.settimeout(1.0)
    rtp_port = rtp.getsockname()[1]
    sdp = CRLF.join([
        "v=0", f"o=lct 1 1 IN IP4 host.docker.internal", "s=LCT RTP smoke",
        "c=IN IP4 host.docker.internal", "t=0 0", f"m=audio {rtp_port} RTP/AVP 0",
        "a=rtpmap:0 PCMU/8000", "a=sendrecv", "",
    ])
    call_id = f"{uuid.uuid4().hex}@lct-smoke"
    from_tag = uuid.uuid4().hex[:10]
    invite_uri = f"sip:7000@{args.host}"
    invite_cseq = 1
    packet = request("INVITE", invite_uri, args.host, sip_port, args.user, call_id, invite_cseq,
                     f"z9hG4bK{uuid.uuid4().hex}", body=sdp, to_user="7000",
                     from_tag=from_tag)
    sip.sendto(packet, (args.host, args.port))
    status, headers, _ = receive_final(sip)
    if "401" in status or "407" in status:
        send_ack(sip, args.host, args.port, args.user, call_id, invite_cseq, "7000",
                 from_tag, headers.get("to"))
        challenge = headers.get("www-authenticate") or headers.get("proxy-authenticate")
        if not challenge:
            raise RuntimeError("INVITE challenge has no authentication header")
        invite_cseq += 1
        auth = digest_header(challenge, args.user, password, "INVITE", invite_uri)
        packet = request("INVITE", invite_uri, args.host, sip_port, args.user, call_id, invite_cseq,
                         f"z9hG4bK{uuid.uuid4().hex}", authorization=auth, body=sdp,
                         to_user="7000", from_tag=from_tag)
        sip.sendto(packet, (args.host, args.port))
        status, answer_headers, answer_sdp = receive_final(sip)
    else:
        answer_headers = headers
        answer_sdp = ""
    if "200" not in status:
        raise RuntimeError(f"INVITE failed: {status}")
    send_ack(sip, args.host, args.port, args.user, call_id, invite_cseq, "7000",
             from_tag, answer_headers.get("to"))

    match = re.search(r"^m=audio\s+(\d+)", answer_sdp, re.MULTILINE)
    if not match:
        raise RuntimeError("SIP 200 response contains no RTP audio port")
    asterisk_rtp_port = int(match.group(1))
    target = (args.host, asterisk_rtp_port + args.rtp_host_offset)
    ssrc = random.getrandbits(32)
    sequence = random.getrandbits(16)
    timestamp = random.getrandbits(32)
    samples: list[float] = []

    # Prime symmetric RTP, then measure matched echoed sequence numbers.
    for index in range(args.packets + 3):
        seq = (sequence + index) & 0xFFFF
        packet = struct.pack("!BBHII", 0x80, 0x00, seq, (timestamp + index * 160) & 0xFFFFFFFF, ssrc)
        marker = struct.pack("!I", index)
        packet += marker + b"\xff" * 156
        started = time.perf_counter()
        rtp.sendto(packet, target)
        deadline = started + 0.5
        while time.perf_counter() < deadline:
            try:
                response, _ = rtp.recvfrom(2048)
            except socket.timeout:
                break
            if len(response) >= 16 and response[12:16] == marker:
                if index >= 3:
                    samples.append((time.perf_counter() - started) * 1000)
                break
            rtp.settimeout(max(0.001, deadline - time.perf_counter()))
        rtp.settimeout(0.5)
        time.sleep(0.02)

    if not samples:
        raise RuntimeError(f"No echoed RTP received from {target}")
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    result = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sip_registration": "ok",
        "sip_echo_call": "ok",
        "rtp_packets_received": len(samples),
        "rtp_rtt_ms_median": round(statistics.median(samples), 3),
        "rtp_rtt_ms_p95": round(p95, 3),
        "requirement_ms": 150,
        "passed": p95 <= 150,
    }

    bye = request(
        "BYE", invite_uri, args.host, sip_port, args.user, call_id, invite_cseq + 1,
        f"z9hG4bK{uuid.uuid4().hex}", to_user="7000", from_tag=from_tag,
        to_header=answer_headers.get("to"),
    )
    sip.sendto(bye, (args.host, args.port))
    bye_status, _, _ = receive_final(sip)
    result["sip_hangup"] = "ok" if "200" in bye_status else bye_status
    result["passed"] = result["passed"] and result["sip_hangup"] == "ok"
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
