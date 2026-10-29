FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AI_TEAM_DB_PATH=/app/data/ai-team.sqlite3 \
    AI_TEAM_REGISTRY_PATH=/app/config/projects.json \
    APP_ENV=production

WORKDIR /app
COPY pyproject.toml README.md ./
COPY ai_team_control_plane ./ai_team_control_plane
COPY config ./config
COPY docs ./docs
RUN pip install --no-cache-dir . \
    && mkdir -p /app/data \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "ai_team_control_plane.api:app", "--host", "0.0.0.0", "--port", "8000"]
