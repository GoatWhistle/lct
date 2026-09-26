#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="lct-webrtc-smoke-$$-${RANDOM:-0}"
compose=(docker compose -p "$project" \
  -f "$repo_root/docker-compose.yml" \
  -f "$repo_root/docker-compose.tls.yml" \
  -f "$repo_root/docker-compose.sip.yml" \
  -f "$repo_root/docker-compose.webrtc-test.yml")
temp_root="$(mktemp -d /tmp/lct-webrtc-smoke-XXXXXX)"

# Keep all ports in one checked block; RTP gets its own 100-port block.
base_port=$((30000 + ($$ % 15000)))
export COMPOSE_PROJECT_NAME="$project"
export BIND_HOST=127.0.0.1
export POSTGRES_PORT=$base_port
export BACKEND_PORT=$((base_port + 1))
export FRONTEND_PORT=$((base_port + 2))
export TLS_PORT=$((base_port + 3))
export SIP_PORT=$((base_port + 4))
export SIPS_PORT=$((base_port + 5))
export SIP_WS_PORT=$((base_port + 6))
export RTP_PORT_START=$((base_port + 100))
export RTP_PORT_END=$((base_port + 199))
export SIP_RTP_START="$RTP_PORT_START"
export SIP_RTP_END="$RTP_PORT_END"
export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export SESSION_SECRET="$(openssl rand -hex 48)"
export TLS_CERT_DIR="$temp_root/frontend-tls"
export SIP_TLS_DIR="$temp_root/sip-tls"
export SIP_EXTERNAL_MEDIA_ADDRESS=127.0.0.1
mkdir -p "$TLS_CERT_DIR" "$SIP_TLS_DIR"

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if ((status != 0)); then
    failure_log="/tmp/${project}-compose.log"
    "${compose[@]}" logs --no-color --tail 250 >"$failure_log" 2>&1 || true
    printf 'Isolated WebRTC diagnostics saved to %s\n' "$failure_log" >&2
  fi
  "${compose[@]}" down --volumes --remove-orphans >/dev/null || true
  case "$temp_root" in
    /tmp/lct-webrtc-smoke-*) rm -rf -- "$temp_root" ;;
    *) printf 'Refusing to remove unexpected temporary path: %s\n' "$temp_root" >&2 ;;
  esac
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

python3 -c 'import socket,sys
ports=[("tcp",int(sys.argv[1])),("tcp",int(sys.argv[2])),("tcp",int(sys.argv[3])),
       ("tcp",int(sys.argv[4])),("tcp",int(sys.argv[5])),("udp",int(sys.argv[5])),
       ("tcp",int(sys.argv[6])),("tcp",int(sys.argv[7]))]
ports.extend(("udp",p) for p in range(int(sys.argv[8]),int(sys.argv[9])+1))
sockets=[]
try:
    for kind,port in ports:
        sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM if kind=="tcp" else socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1",port)); sockets.append(sock)
except OSError as exc:
    raise SystemExit(f"isolated WebRTC smoke port {port}/{kind} unavailable: {exc}")
finally:
    [sock.close() for sock in sockets]' \
  "$POSTGRES_PORT" "$BACKEND_PORT" "$FRONTEND_PORT" "$TLS_PORT" "$SIP_PORT" \
  "$SIPS_PORT" "$SIP_WS_PORT" "$RTP_PORT_START" "$RTP_PORT_END"

printf 'Starting isolated WebRTC smoke project: %s\n' "$project"
cd "$repo_root"
npm --prefix frontend run build
"${compose[@]}" build backend sip
"${compose[@]}" up --no-build --pull never --detach --wait --wait-timeout 300 \
  postgres backend frontend sip

python3 "$repo_root/scripts/smoke_webrtc_browser.py" \
  --frontend-url "https://127.0.0.1:$TLS_PORT" \
  --backend-url "http://127.0.0.1:$BACKEND_PORT" \
  --database-url "postgresql+asyncpg://lct:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT}/lct" \
  --timeout "${WEBRTC_TEST_TIMEOUT:-60}" \
  --output "$repo_root/docs/evidence/webrtc-browser-isolated-2026-09-26.json"
