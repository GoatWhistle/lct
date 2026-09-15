FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/usr/local PYTHONUNBUFFERED=1

COPY pyproject.toml ./
RUN uv pip install --system fastapi "uvicorn[standard]" "pydantic>=2.7" pydantic-settings \
    "sqlalchemy[asyncio]" alembic asyncpg httpx pyyaml structlog

COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
