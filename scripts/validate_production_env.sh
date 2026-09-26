#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${POSTGRES_PASSWORD+x}" ]]; then
  password="$POSTGRES_PASSWORD"
elif [[ -f "$repo_root/.env" ]]; then
  matches="$(grep -c '^POSTGRES_PASSWORD=' "$repo_root/.env" || true)"
  if [[ "$matches" != "1" ]]; then
    echo "Укажите ровно один POSTGRES_PASSWORD в .env или окружении." >&2
    exit 2
  fi
  password="$(sed -n 's/^POSTGRES_PASSWORD=//p' "$repo_root/.env")"
else
  echo "Для production запуска скопируйте .env.example в .env и задайте POSTGRES_PASSWORD." >&2
  exit 2
fi

if [[ ! "$password" =~ ^[A-Za-z0-9._~-]{32,}$ ]]; then
  echo "POSTGRES_PASSWORD должен содержать не менее 32 символов: латиница, цифры, точка, дефис, подчёркивание или тильда. Значение не показано." >&2
  exit 2
fi

echo "Production DB secret задан и удовлетворяет формату (само значение не отображается)."
