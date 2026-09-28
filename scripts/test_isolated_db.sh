#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
container="lct-test-$$-$RANDOM$RANDOM"

cleanup() {
  trap - EXIT INT TERM
  docker rm --force --volumes "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Временная БД без compose-файла и без тома: dev-база не затрагивается,
# порт на loopback выбирает Docker.
printf 'Изолированная тестовая PostgreSQL: %s\n' "$container"
docker run --detach --rm --name "$container" \
  --env POSTGRES_USER=lct_test --env POSTGRES_PASSWORD=lct_test --env POSTGRES_DB=lct_test \
  --publish 127.0.0.1::5432 \
  --health-cmd 'pg_isready -U lct_test -d lct_test' --health-interval 2s --health-retries 30 \
  postgres:16-alpine >/dev/null
for _ in $(seq 60); do
  status=$(docker inspect --format '{{.State.Health.Status}}' "$container")
  [[ $status == healthy ]] && break
  sleep 1
done
[[ $status == healthy ]] || { echo "PostgreSQL не поднялась: $status" >&2; exit 1; }
port_mapping=$(docker port "$container" 5432/tcp | head -n1)
db_port="${port_mapping##*:}"
database_url="postgresql+asyncpg://lct_test:lct_test@127.0.0.1:${db_port}/lct_test"
evidence_dir="${LCT_TEST_EVIDENCE_DIR:-$repo_root/.local/evidence}"
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
