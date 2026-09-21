FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONUNBUFFERED=1

# Зависимости — из pyproject.toml, а не отдельным списком: ручной список разошёлся
# с проектом, и бэкенд в контейнере упал на первом же новом пакете (num2words).
# Голосовой контур (группа voice: torch, onnx-asr) в образ не входит: под WSL
# модели запускаются нативно — так они и мерялись (docs/LATENCY.md).
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml

COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
