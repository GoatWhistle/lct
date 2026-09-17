#!/usr/bin/env bash
# make demo: поднять стенд для занятия или показа.
#
# Postgres в контейнере, бэкенд и фронт — нативно: модели речи под WSL работают
# нативно, так они и мерялись (docs/LATENCY.md). Скрипт обкатывается заранее,
# а не пишется в последнюю ночь: им поднимают стенд в классе.
set -euo pipefail
cd "$(dirname "$0")/.."

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/5 Модели"
missing=0
for path in backend/models/e5-small/model_quantized.onnx \
            backend/models/gigaam-v3-onnx/v3_rnnt_encoder.int8.onnx \
            backend/models/silero-vad/silero_vad.onnx \
            backend/models/silero-tts/v5_ru.pt; do
    if [ ! -f "$path" ]; then echo "  нет $path"; missing=1; fi
done
if [ "$missing" = 1 ]; then
    echo "  запусти: make models   (Silero TTS качается вручную, скрипт подскажет)"
    exit 1
fi
echo "  все модели на месте"

say "2/5 База"
docker compose up -d postgres
( cd backend && uv run alembic upgrade head >/dev/null )
echo "  миграции накатаны"

say "3/5 Сценарии"
make seed

say "4/5 Бэкенд с голосом"
# Контейнерные бэкенд и фронт гасим: порты те же, а голос работает нативно.
docker compose stop backend frontend >/dev/null 2>&1 || true
# Подоболочка явно: `cd backend && команда &` уводит в фон всю связку,
# а родительская оболочка остаётся на месте — следующий `cd ..` увёл бы её
# из репозитория, и фронт запускался бы не оттуда.
( cd backend && OFFLINE=true exec uv run --extra voice uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 ) &
BACKEND=$!
trap 'kill $BACKEND $FRONTEND 2>/dev/null || true' EXIT INT TERM

for _ in $(seq 1 60); do
    if curl -sf localhost:8000/api/health | grep -q '"models_ready":true'; then break; fi
    sleep 2
done
curl -s localhost:8000/api/health; echo

say "5/5 Фронт"
npm --prefix frontend run dev -- --host 0.0.0.0 &
FRONTEND=$!

say "Стенд поднят"
cat <<'LINKS'
  пульт преподавателя: http://localhost:5173/instructor
  профили курсантов:   http://localhost:5173/profile
  ссылки на АРМ курсанта, монитор и ДДС печатает сам пульт

  Ctrl+C гасит стенд.
LINKS
wait $BACKEND
