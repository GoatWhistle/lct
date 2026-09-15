# Подъём стенда и рутина. Цели, которых ещё нет, честно падают
# и называют карточку, которая их закрывает, — README не должен врать.

UV ?= uv
COMPOSE ?= docker compose

.DEFAULT_GOAL := help

help:                     ## Список целей
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t22

dev: ## Поднять стенд: postgres + backend --reload + frontend
	$(COMPOSE) up --build

down: ## Погасить стенд
	$(COMPOSE) down

back: ## Только бэкенд, локально, без докера
	cd backend && $(UV) run uvicorn app.main:app --reload --port 8000

front: ## Только фронтенд, локально
	npm --prefix frontend install && npm --prefix frontend run dev

types: ## domain/events.py → frontend/src/shared/types/generated.ts
	cd backend && $(UV) run --no-project --with 'pydantic>=2.7' python scripts/export_types.py

test: ## Тесты бэкенда
	cd backend && $(UV) run --extra dev pytest -q

typecheck: ## Проверить фронтенд компилятором
	npm --prefix frontend run typecheck

models: ## Скачать веса GigaAM (STT) и Silero (TTS) в backend/models/
	@echo "не реализовано — карточка tasks/lct-02-latency-baseline.md"; exit 1

seed: ## Залить сценарии из /scenarios в БД
	@echo "не реализовано — карточка tasks/lct-04-scenario-loader.md"; exit 1

repl: ## Текстовый диалог со звонящим без голоса
	@echo "не реализовано — карточка tasks/lct-07-caller-slots.md"; exit 1

pregen: ## Дерево диалога и WAV первых реплик для офлайна
	@echo "не реализовано — карточка tasks/lct-19-reference-dialog.md"; exit 1

demo: ## Поднять всё в демо-режиме: офлайн, прогретые модели
	@echo "не реализовано — карточка tasks/lct-21-demo-readiness.md"; exit 1

.PHONY: help dev down back front types test typecheck models seed repl pregen demo
