FROM python:3.11-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Экспорт PDF требует локальный TTF с кириллицей; базовый slim-образ его не
# гарантирует. Шрифт запекается в образ при подготовке офлайн-комплекта.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Зависимости — из pyproject.toml, а не отдельным списком: ручной список разошёлся
# с проектом, и бэкенд в контейнере упал на первом же новом пакете (num2words).
# Голосовой контур (группа voice: torch, onnx-asr) в стадию base не входит —
# его вместе с весами добавляет стадия voice ниже.
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml

COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Прод-образ стенда: голосовой контур, сценарии и веса внутри, bind-mount'ов нет.
# Веса и сценарии приходят именованными контекстами сборки (docker-bake.hcl,
# docker-compose.yml): models=backend/models, scenarios=scenarios,
# licenses=deploy/licenses. В Dockerfile ничего не скачивается — хост Silero
# ненадёжен; нет файла — COPY падает с его именем, `make images` проверяет заранее.
# Whisper в образ не входит: единственный проверенный вживую STT — GigaAM.
FROM base AS voice
# torch только CPU-сборка: обычный PyPI тянет CUDA на гигабайты (pyproject).
RUN uv pip install --system --extra-index-url https://download.pytorch.org/whl/cpu \
    'torch>=2.2' 'onnx-asr>=0.6' 'scipy>=1.11'
COPY --from=scenarios . /scenarios
# Из GigaAM — только RNNT int8: CTC-вариант стенд не загружает.
COPY --from=models gigaam-v3-onnx/config.json gigaam-v3-onnx/v3_vocab.txt \
    gigaam-v3-onnx/v3_rnnt_encoder.int8.onnx gigaam-v3-onnx/v3_rnnt_decoder.int8.onnx \
    gigaam-v3-onnx/v3_rnnt_joint.int8.onnx /app/models/gigaam-v3-onnx/
COPY --from=models silero-vad/silero_vad.onnx /app/models/silero-vad/
COPY --from=models silero-tts/v5_ru.pt /app/models/silero-tts/
COPY --from=models e5-small/config.json e5-small/model_quantized.onnx e5-small/tokenizer.json \
    /app/models/e5-small/
COPY --from=licenses . /licenses
