FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false \
    LAYA_DECISION_DEVICE=cpu LAYA_DECISION_THREADS=2 \
    LAYA_DECISION_MODEL_PATH=/models/model LAYA_DECISION_TOKEN_FILE=/run/secrets/decision_token
WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.9 \
    && groupadd --gid 10001 decision \
    && useradd --uid 10001 --gid decision --no-create-home decision
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-cache --no-install-project
COPY decision_service ./decision_service
USER decision
EXPOSE 8791
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8791/healthz', timeout=3)"]
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "decision_service.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8791", "--workers", "1", "--limit-concurrency", "16", "--no-access-log"]
