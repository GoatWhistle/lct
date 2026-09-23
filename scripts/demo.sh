#!/usr/bin/env bash
# make demo: поднять стенд для занятия или показа.
#
# Postgres в контейнере, бэкенд и фронт — нативно. В основном упражнении ДДС
# голосовой контур выключен; VOICE_ENABLED=true — только отдельный эксперимент.
set -euo pipefail
cd "$(dirname "$0")/.."

# Не конфликтуем с уже установленным PostgreSQL. Один POSTGRES_PORT меняет и
# проброс контейнера, и адрес нативного backend; явный DATABASE_URL приоритетен.
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://lct:lct@127.0.0.1:${POSTGRES_PORT}/lct}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/5 Модели"
if [ "${VOICE_ENABLED:-false}" = "true" ]; then
    missing=0
    for path in backend/models/e5-small/model_quantized.onnx \
                backend/models/whisper-small/ggml-small-q5_1.bin \
                backend/models/silero-vad/silero_vad.onnx \
                backend/models/silero-tts/v5_ru.pt; do
        if [ ! -f "$path" ]; then echo "  нет $path"; missing=1; fi
    done
    if [ "$missing" = 1 ]; then
        echo "  запусти: make models и make local-models; Silero TTS положи отдельно"
        exit 1
    fi
    if ! curl -fsS --max-time 2 http://127.0.0.1:18082/ >/dev/null; then
        echo "  нет whisper-server: запусти make local-stt в отдельном терминале"
        exit 1
    fi
    echo "  голосовой контур включён"
else
    echo "  MVP ДДС: STT и голосовой звонок 112 выключены"
fi

say "2/5 База"
docker compose up -d postgres
( cd backend && uv run alembic upgrade head >/dev/null )
echo "  миграции накатаны"

say "3/5 Сценарии и учётные записи"
make seed
make users

say "4/5 Бэкенд"
# Контейнерные бэкенд и фронт гасим: порты те же.
docker compose stop backend frontend >/dev/null 2>&1 || true
# Подоболочка явно: `cd backend && команда &` уводит в фон всю связку,
# а родительская оболочка остаётся на месте — следующий `cd ..` увёл бы её
# из репозитория, и фронт запускался бы не оттуда.
( cd backend && OFFLINE=true VOICE_ENABLED="${VOICE_ENABLED:-false}" exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 ) &
BACKEND=$!
FRONTEND=
trap 'kill $BACKEND $FRONTEND 2>/dev/null || true' EXIT INT TERM

for _ in $(seq 1 60); do
    if curl -sf localhost:8000/api/health | grep -q '"status":"ok"'; then break; fi
    sleep 2
done
curl -s localhost:8000/api/health; echo

say "5/5 Фронт"
npm --prefix frontend run dev -- --host 0.0.0.0 &
FRONTEND=$!

say "Стенд поднят"
cat <<'LINKS'
  вход:                http://localhost:5173/  (пароли печатает make users)
  пульт преподавателя: http://localhost:5173/instructor
  профили курсантов:   http://localhost:5173/profile
  ссылки на АРМ курсанта, монитор и ДДС печатает сам пульт

  Ctrl+C гасит стенд.
LINKS
wait $BACKEND
