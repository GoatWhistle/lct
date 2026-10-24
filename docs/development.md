# Разработка

## Команды

| Команда | Что делает |
|---|---|
| `make dev` | База, backend, фронт, Asterisk, Qwen и Vikhr в Docker, то же, что `docker compose up` |
| `make users-docker` | Учётки admin, teacher, trainee; пароли печатаются один раз |
| `make offline` | Поднимает стенд без обращения к сети, когда образы уже скачаны |
| `make demo` | База в Docker, backend и фронт нативно, готовые карточки |
| `make test` | Тесты бэкенда |
| `make test-db` | Полный прогон на временной PostgreSQL |
| `make typecheck` | Проверка фронта |
| `make types` | Pydantic → TypeScript |
| `make sip` | Только Asterisk |
| `make sip-test`, `make webrtc-test` | Приёмка связи |
| `make images` | Собрать 4 образа стенда (`docker-bake.hcl`) |

## Показ без Docker, базы и моделей

```bash
make demo-lite
VITE_DEMO_MODE=true VITE_BACKEND_URL=http://127.0.0.1:8112 VITE_WS_BACKEND_URL=ws://127.0.0.1:8112 \
  npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173
```

Вход — «Войти в демо одним нажатием». Данные живут в памяти до остановки backend; история,
аналитика группы и голос в этом режиме недоступны.

## Контракт фронта и бэка

Pydantic-модели в [`backend/app/domain/`](../backend/app/domain/) описывают КИО, события,
таймеры, статусы ДДС, ЕКП и таксономию ошибок. TypeScript-типы фронта генерируются из них
(`make types`), расхождение ловит тест. Курсант не получает ни фактов сценария, ни эталона:
их не видно даже в DevTools.

## Модели и лицензии весов

Веса моделей не изменялись, лицензии лежат в [`deploy/licenses/`](../deploy/licenses/NOTICE.md):

- Qwen3 — Apache 2.0;
- GigaAM и Silero VAD — MIT;
- Silero TTS — CC BY-NC-SA 4.0, только некоммерческое использование;
- Vikhr-Llama-3.2-1B-Instruct — дообучение Llama 3.2, Llama 3.2 Community License,
  © Meta Platforms, Inc. Built with Llama.
