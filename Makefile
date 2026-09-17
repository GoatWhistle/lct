# Подъём стенда и рутина. Цели, которых ещё нет, честно падают
# и называют карточку, которая их закрывает, — README не должен врать.

UV ?= uv

# `make test` и `make demo` не запускать одновременно: они просят у uv разные
# группы зависимостей, и uv пересобирает одно и то же окружение, а второй запуск
# молча ждёт блокировку. Один раз поставить оба набора:
#   cd backend && uv sync --extra dev --extra voice
COMPOSE ?= docker compose

.DEFAULT_GOAL := help

help:                     ## Список целей
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

dev: ## Поднять стенд: postgres + backend --reload + frontend
	$(COMPOSE) up --build

down: ## Погасить стенд
	$(COMPOSE) down

back: ## Бэкенд нативно, с голосовым контуром (порт 8000: сначала docker compose stop backend)
	cd backend && $(UV) run --extra voice uvicorn app.main:app --reload --port 8000 --workers 1

front: ## Только фронтенд, локально
	npm --prefix frontend install && npm --prefix frontend run dev

types: ## domain/events.py → frontend/src/shared/types/generated.ts
	cd backend && $(UV) run --no-project --with 'pydantic>=2.7' python scripts/export_types.py

test: ## Тесты бэкенда (голосовой контур пропускается)
	cd backend && $(UV) run --extra dev pytest -q

test-voice: ## Тест голосового контура на настоящих моделях: задержка и перебивание
	cd backend && $(UV) run --extra dev --extra voice pytest tests/test_voice_pipeline.py -q -s

typecheck: ## Проверить фронтенд компилятором
	npm --prefix frontend run typecheck

models: ## Скачать модели в backend/models/: эмбеддинги, GigaAM, Silero VAD; Silero TTS — вручную
	cd backend && $(UV) run python scripts/models.py

seed: ## Залить сценарии из /scenarios в БД
	cd backend && $(UV) run python scripts/seed.py

lesson: ## Запустить занятие и напечатать ссылки: make lesson s=<сценарий> m=<режим>
	cd backend && $(UV) run --no-project --with websockets python scripts/start_lesson.py "$(s)" "$(m)"

latency: ## Замер задержки голосового контура по этапам — запускать на демо-машине
	cd backend && $(UV) run --extra voice python scripts/latency.py

migrate: ## Накатить миграции
	cd backend && $(UV) run alembic upgrade head

revision: ## Создать миграцию: make revision m="что изменилось"
	cd backend && $(UV) run alembic revision --autogenerate -m "$(m)"

repl: ## Текстовый диалог со звонящим без голоса: make repl s=<сценарий>
	cd backend && $(UV) run python scripts/repl.py "$(s)"

pregen: ## Дерево диалога и WAV первых реплик для офлайна
	@echo "не реализовано — карточка tasks/lct-19-reference-dialog.md"; exit 1

demo: ## Поднять стенд для занятия: база, сценарии, бэкенд с голосом, фронт
	./scripts/demo.sh

.PHONY: help dev down back front types test test-voice typecheck lesson latency migrate revision models seed repl pregen demo
