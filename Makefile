# Подъём стенда и рутина. Цели, которых ещё нет, честно падают
# и называют карточку, которая их закрывает, — README не должен врать.

UV ?= uv

# `make test` и `make demo` не запускать одновременно: они просят у uv разные
# группы зависимостей, и uv пересобирает одно и то же окружение, а второй запуск
# молча ждёт блокировку. Один раз поставить оба набора:
#   cd backend && uv sync --extra dev --extra voice
COMPOSE ?= docker compose
DEMO_PORT ?= 8112
# Веса, без которых `make images` не соберёт backend и llm: COPY в Dockerfile
# упал бы на первом же отсутствующем файле, здесь — список всех сразу понятнее.
IMAGE_WEIGHTS := qwen3-1.7b/Qwen3-1.7B-Q8_0.gguf vikhr-1b/Vikhr-Llama-3.2-1B-Q4_K_M.gguf \
	gigaam-v3-onnx/config.json gigaam-v3-onnx/v3_vocab.txt gigaam-v3-onnx/v3_rnnt_encoder.int8.onnx \
	gigaam-v3-onnx/v3_rnnt_decoder.int8.onnx gigaam-v3-onnx/v3_rnnt_joint.int8.onnx \
	silero-vad/silero_vad.onnx silero-tts/v5_ru.pt \
	e5-small/config.json e5-small/model_quantized.onnx e5-small/tokenizer.json

.DEFAULT_GOAL := help

help:                     ## Список целей
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

dev: ## Поднять полный стенд из готовых образов: http://127.0.0.1:5173
	$(COMPOSE) up

offline: ## То же без обращения к registry: образы уже скачаны или загружены
	$(COMPOSE) up --pull never --no-build

images: ## Собрать 4 образа стенда (docker-bake.hcl); веса — из backend/models
	@for f in $(IMAGE_WEIGHTS); do test -s backend/models/$$f \
		|| { echo "нет весов backend/models/$$f — make local-models и make models"; exit 2; }; done
	docker buildx bake

sip: ## Поднять только локальный Asterisk SIP/VoIP
	$(COMPOSE) up -d sip

offline-sip: ## Поднять SIP из уже скачанного образа без сети
	$(COMPOSE) up --pull never --no-build -d sip

sip-credentials: ## Показать локальные пароли софтфонов 6001–6003 и WebRTC 6101–6103
	$(COMPOSE) exec -T sip cat /var/lib/lct-sip/credentials.env

sip-test: ## Проверить регистрацию, звонок, RTP и задержку (args="--rtp-host-offset 1000")
	python3 scripts/smoke_sip.py --port $(or $(SIP_PORT),5060) $(args)

webrtc-test: ## Проверить два браузерных SIP-клиента, аудио и запись Asterisk
	python3 scripts/smoke_webrtc_browser.py \
		--frontend-url http://127.0.0.1:$(or $(FRONTEND_PORT),5173) \
		--backend-url http://127.0.0.1:$(or $(BACKEND_PORT),8000) \
		--database-url postgresql+asyncpg://lct:lct@127.0.0.1:$(or $(POSTGRES_PORT),5432)/lct $(args)

down: ## Погасить стенд
	$(COMPOSE) down

back: ## Бэкенд нативно, без голоса по умолчанию (порт 8000: сначала docker compose stop backend)
	cd backend && $(UV) run --extra voice uvicorn app.main:app --reload --port 8000 --workers 1

front: ## Только фронтенд, локально
	npm --prefix frontend install && npm --prefix frontend run dev

types: ## domain/events.py → frontend/src/shared/types/generated.ts
	cd backend && $(UV) run --no-project --with 'pydantic>=2.7' python scripts/export_types.py

test: ## Тесты бэкенда (голосовой контур пропускается)
	cd backend && $(UV) run --extra dev pytest -q

test-db: ## Полный backend-прогон на новой временной PostgreSQL, без касания dev-базы
	bash scripts/test_isolated_db.sh $(args)

test-voice: ## Тест голосового контура на настоящих моделях: задержка и перебивание
	cd backend && $(UV) run --extra dev --extra voice pytest tests/test_voice_pipeline.py -q -s

typecheck: ## Проверить фронтенд компилятором
	npm --prefix frontend run typecheck

models: ## Скачать эмбеддинги и Silero VAD; Silero TTS — вручную
	cd backend && $(UV) run python scripts/models.py

local-models: ## Скачать и проверить Qwen3 1.7B, Vikhr 1B, Whisper small (нужна сеть один раз)
	cd backend && $(UV) run --offline --no-sync python scripts/download_local_models.py

local-llm: ## Запустить Qwen3 на localhost:18080 (mode=both для Qwen3 и Vikhr)
	cd backend && python3 scripts/local_llms.py --mode $(or $(mode),dialogue)

local-stt: ## Запустить локальный whisper.cpp server на localhost:18082
	cd backend && python3 scripts/local_stt.py

backup: ## Резервная копия базы в backend/backups/ (для cron: раз в сутки)
	cd backend && $(UV) run python scripts/backup.py

users: ## Завести учётные записи ролей и напечатать пароли: make users force=1
	cd backend && $(UV) run python scripts/users.py

users-docker: ## Завести учётные записи внутри локального контейнера (после make offline)
	$(COMPOSE) exec backend python scripts/users.py

seed: ## Залить сценарии из /scenarios в БД
	cd backend && $(UV) run python scripts/seed.py

lesson: ## Запустить занятие и напечатать ссылки: make lesson s=<сценарий> m=<режим>
	cd backend && $(UV) run --no-project --with websockets python scripts/start_lesson.py "$(s)" "$(m)"

test-llm-local: ## Живые проверки локального Qwen по backend/.env.test (без сети)
	test -f backend/.env.test || (echo "Создайте backend/.env.test из backend/.env.test.example"; exit 2)
	cd backend && $(UV) run --offline --extra dev pytest tests/test_llm.py -m llm -q -s

test-llm: test-llm-local ## Совместимое имя цели локальной проверки LLM

llm-check: ## Один запрос к активной локальной модели из backend/.env
	cd backend && $(UV) run python scripts/llm_check.py

latency: ## Замер задержки голосового контура по этапам — запускать на демо-машине
	cd backend && $(UV) run --extra voice python scripts/latency.py

migrate: ## Накатить миграции
	cd backend && $(UV) run alembic upgrade head

revision: ## Создать миграцию: make revision m="что изменилось"
	cd backend && $(UV) run alembic revision --autogenerate -m "$(m)"

repl: ## Текстовый диалог со звонящим без голоса: make repl s=<сценарий>
	cd backend && $(UV) run python scripts/repl.py "$(s)"

pregen: ## Офлайн-таблицы реплик: make pregen [s=<сценарий>] [t=<билет>] [force=1]
	cd backend && s="$(s)" t="$(t)" force="$(force)" $(UV) run python scripts/pregenerate.py

demo: ## Поднять основной стенд ДДС: база, готовые карточки, бэкенд и фронт
	./scripts/demo.sh

demo-lite: ## Локальная демонстрация карточки/ДДС без Docker, БД, голоса и внешней сети
	cd backend && UV_CACHE_DIR=/tmp/lct-uv-demo-cache OFFLINE=true VOICE_ENABLED=false DEV_AUTH_BYPASS=true DEMO_NO_DB=true $(UV) run --offline --no-sync uvicorn app.main:app --host 127.0.0.1 --port $(DEMO_PORT) --workers 1

.PHONY: help dev offline images sip offline-sip sip-credentials sip-test webrtc-test down back front types users users-docker backup test test-db test-voice typecheck test-llm test-llm-local lesson llm-check latency migrate revision models local-models local-llm local-stt seed repl pregen demo demo-lite
