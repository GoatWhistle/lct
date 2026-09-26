#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="lct-production-check-$$"
port=$((20000 + $$ % 40000))
secret="prod-check-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
compose=(docker compose -p "$project" -f "$repo_root/docker-compose.yml" \
  -f "$repo_root/docker-compose.production.yml")

cleanup() {
  POSTGRES_PASSWORD="$secret" POSTGRES_PORT="$port" \
    "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

if env -u POSTGRES_PASSWORD "${compose[@]}" config --quiet >/dev/null 2>&1; then
  echo "ОШИБКА: production Compose запустился без POSTGRES_PASSWORD" >&2
  exit 1
fi

POSTGRES_PASSWORD="$secret" POSTGRES_PORT="$port" \
  "${compose[@]}" up --pull never --detach --wait postgres

network="${project}_default"
docker run --pull never --rm --network "$network" -e "PGPASSWORD=$secret" \
  postgres:16-alpine psql -h postgres -U lct -d lct -Atc 'select 1' | rg -x '1' >/dev/null

if docker run --pull never --rm --network "$network" -e PGPASSWORD=wrong \
  postgres:16-alpine psql -h postgres -U lct -d lct -Atc 'select 1' >/dev/null 2>&1; then
  echo "ОШИБКА: production PostgreSQL принял неверный пароль" >&2
  exit 1
fi

echo "Production PostgreSQL принял пароль из backend-сети и отклонил неверный."
