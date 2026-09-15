---
id: "lct-04-scenario-loader"
title: "Библиотека сценариев: схема, загрузчик, make seed"
owner: "бэкенд-2 + лид"
priority: must
depends_on: [lct-01]
blocks: [lct-07, lct-12, lct-19]
estimate: "1 день"
---

# Сценарии: схема, валидация, загрузка

## Зачем

Сценарии пишет методист, не программист. Сломанный YAML, найденный посреди занятия, —
сценарий, которого не должно случиться: загрузчик обязан падать на старте приложения
с внятным сообщением.

## Что сделать

- `scenarios/schema.py` — Pydantic-схема по `docs/spec/SCENARIO-FORMAT.md`.
- `loader.py` — чтение YAML из `/scenarios`, `extends` для чек-листов по классификаторам,
  валидация **всех** файлов на старте.
- `ground_truth` выводится из `facts` кодом, а не читается из YAML: иначе генератор
  рассинхронизирует факты и эталон и курсант получит штраф за правильный ответ.
- Проверки: факт с `hidden: true` без `reveal_on`, `checklist` со ссылкой на несуществующий
  факт, `era_glonass` без `type: era_glonass`.
- `make seed` — заливка в БД. `GET /api/scenarios/{id}` **не отдаёт** `facts` и `ground_truth`.

## Готово когда

- [x] `scenarios/fire-apartment-l2.yaml` грузится, битый YAML роняет старт с понятным текстом
- [x] `ground_truth` собран кодом и совпадает с фактами
- [x] В ответе `GET /api/scenarios/{id}` нет ни адреса, ни числа пострадавших (проверено через DevTools)

## Ссылки

- [docs/spec/SCENARIO-FORMAT.md](../docs/spec/SCENARIO-FORMAT.md)
- [scenarios/fire-apartment-l2.yaml](../scenarios/fire-apartment-l2.yaml)
