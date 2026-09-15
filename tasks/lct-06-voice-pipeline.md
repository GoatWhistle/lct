---
id: "lct-06-voice-pipeline"
title: "Голосовой контур: VAD → STT → TTS, barge-in"
owner: "бэкенд-архитектор"
priority: must
depends_on: [lct-01, lct-02, lct-05]
blocks: [lct-09]
estimate: "3 дня"
---

# Голосовой контур

## Зачем

Критический путь всего проекта. Всё остальное наращивается поверх работающего контура.

## Что сделать

- `voice/pipeline.py` — четыре asyncio-задачи через очереди, отмена по `CancelledError`
  по всей цепочке сразу.
- `voice/vad.py` — Silero VAD, endpointing **600 мс** (не подбирать заново), детект barge-in.
- `voice/stt.py` — GigaAM через `onnx-asr`, режим по решению из lct-02.
- `voice/tts.py` — Silero v5 + SSML, чанки по предложению.
- `voice/audio.py` — PCM16, кадры 20 мс на вход (16 кГц), выход 24 кГц.
- `voice/prefetch.py` — предгенерённый WAV первой реплики и филлеры («алло?..» при задержке >1 с).
- Бинарные кадры по `/ws/call`, без обёртки JSON. `tts.cancel` при перебивании.

## Готово когда

- [ ] Задержка от конца реплики оператора до первого звука ответа ≤ 1.5 с, **измерена и записана**
- [ ] Barge-in гасит звук под 150 мс и отменяет LLM и TTS одним движением
- [ ] Первая реплика играет мгновенно (предгенерённый WAV)
- [ ] Прогон сценария 5 раз подряд без падений

## Ссылки

- [docs/arch/BACKEND.md](../docs/arch/BACKEND.md#пайплайн)
- [docs/arch/CONTRACT.md](../docs/arch/CONTRACT.md#аудио-по-websocket)
