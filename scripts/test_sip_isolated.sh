#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="lct-sip-smoke-$(date +%Y%m%d)-$$-${RANDOM:-0}"
compose=(docker compose -p "$project" -f "$repo_root/docker-compose.yml" -f "$repo_root/docker-compose.sip.yml")

sip_port="${SIP_SMOKE_PORT:-15170}"
sips_port="${SIP_SMOKE_SIPS_PORT:-15171}"
websocket_port="${SIP_SMOKE_WS_PORT:-18098}"
rtp_start="${SIP_SMOKE_RTP_START:-12000}"
rtp_end=$((rtp_start + 99))
rtp_offset=$((rtp_start - 10000))
tls_dir="$(mktemp -d /tmp/lct-sip-smoke-tls-XXXXXX)"
sip_smoke_password="$(openssl rand -hex 24)"

cleanup() {
  trap - EXIT INT TERM
  "${compose[@]}" down --volumes --remove-orphans >/dev/null || true
  case "$tls_dir" in
    /tmp/lct-sip-smoke-tls-*) rm -rf -- "$tls_dir" ;;
    *) printf 'Refusing to remove unexpected TLS temp path: %s\n' "$tls_dir" >&2 ;;
  esac
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

python3 -c 'import socket,sys; ports=[("tcp",int(sys.argv[1])),("udp",int(sys.argv[1])),("tcp",int(sys.argv[2])),("tcp",int(sys.argv[3]))]+[("udp",p) for p in range(int(sys.argv[4]),int(sys.argv[5])+1)]; sockets=[]
try:
    for kind,port in ports:
        sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM if kind=="tcp" else socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1",port)); sockets.append(sock)
except OSError as exc:
    raise SystemExit(f"SIP smoke port {port}/{kind} is unavailable: {exc}")
finally:
    [sock.close() for sock in sockets]' "$sip_port" "$sips_port" "$websocket_port" "$rtp_start" "$rtp_end"

printf 'Starting isolated SIP smoke project: %s\n' "$project"
SIP_6001_PASSWORD="$sip_smoke_password" \
SIP_PORT="$sip_port" SIPS_PORT="$sips_port" SIP_WS_PORT="$websocket_port" \
RTP_PORT_START="$rtp_start" RTP_PORT_END="$rtp_end" \
SIP_TLS_DIR="$tls_dir" SIP_EXTERNAL_MEDIA_ADDRESS=127.0.0.1 \
  "${compose[@]}" up --pull never --no-build --detach --wait --no-deps sip

SIP_PASSWORD="$sip_smoke_password" python3 "$repo_root/scripts/smoke_sip.py" \
  --host 127.0.0.1 --port "$sip_port" --rtp-host-offset "$rtp_offset" \
  --output "$repo_root/docs/evidence/sip-rtp-2026-09-25.json"
