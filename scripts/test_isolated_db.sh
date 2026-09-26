#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="lct-test-$$-$RANDOM$RANDOM"
compose=(docker compose -p "$project" -f "$repo_root/docker-compose.test-db.yml")

cleanup() {
  trap - EXIT INT TERM
  "${compose[@]}" down --volumes --remove-orphans >/dev/null || true
}
trap cleanup EXIT

printf 'Изолированный тестовый Compose project: %s\n' "$project"
"${compose[@]}" up --pull never --detach --wait
port_mapping=$("${compose[@]}" port postgres 5432)
db_port="${port_mapping##*:}"
database_url="postgresql+asyncpg://lct_test:lct_test@127.0.0.1:${db_port}/lct_test"
evidence_dir="${LCT_TEST_EVIDENCE_DIR:-$repo_root/docs/evidence}"
mkdir -p "$evidence_dir"

cd "$repo_root/backend"
DATABASE_URL="$database_url" uv run --extra dev alembic upgrade head
DATABASE_URL="$database_url" uv run --extra dev python scripts/seed.py
DATABASE_URL="$database_url" uv run --extra dev python "$repo_root/scripts/load_db.py" \
  --operations 1000 --workload audit --allow-audit-writes \
  --output "$evidence_dir/db-audit-write-load-pool-2026-09-25.json"
DATABASE_URL="$database_url" uv run --extra dev python "$repo_root/scripts/load_db.py" \
  --operations 1000 --concurrency 20 --workload audit --allow-audit-writes \
  --output "$evidence_dir/db-audit-write-load-concurrent-2026-09-25.json"
if (($#)); then
  DATABASE_URL="$database_url" uv run --extra dev pytest -q --tb=short --show-capture=no "$@"
else
  DATABASE_URL="$database_url" uv run --extra dev pytest -q --tb=short --show-capture=no
fi
