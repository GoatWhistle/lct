#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(docker compose -f "$repo_root/docker-compose.yml" \
  -f "$repo_root/docker-compose.production.yml" -f "$repo_root/docker-compose.tls.yml")

if env -u POSTGRES_PASSWORD "${compose[@]}" config --quiet >/dev/null 2>&1; then
  echo "ОШИБКА: production Compose запустился без POSTGRES_PASSWORD" >&2
  exit 1
fi

secret="compose-check-0123456789abcdef0123456789abcdef"
case "$secret" in
  *[!A-Za-z0-9._~-]*) echo "ОШИБКА: тестовый пароль не URL-safe" >&2; exit 1 ;;
esac
resolved="$(POSTGRES_PASSWORD="$secret" "${compose[@]}" config --format json 2>/dev/null)"
printf '%s' "$resolved" | jq -e --arg secret "$secret" '
  .services.postgres.environment.POSTGRES_PASSWORD == $secret and
  .services.backend.environment.DATABASE_URL == ("postgresql+asyncpg://lct:" + $secret + "@postgres:5432/lct") and
  .services.backup.environment.DATABASE_URL == ("postgresql+asyncpg://lct:" + $secret + "@postgres:5432/lct") and
  .services.backend.environment.APP_ENV == "production" and
  .services.backend.environment.DEMO_NO_DB == "false" and
  .services.backend.environment.DEV_AUTH_BYPASS == "false" and
  .services.backend.environment.SECURE_COOKIES == "true"
' >/dev/null
echo "Production Compose требует уникальный пароль и передаёт его PostgreSQL/backend/backup."
