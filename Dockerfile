FROM node:24-alpine AS web-build

WORKDIR /src
COPY web/package.json web/package-lock.json ./web/
RUN npm --prefix web ci
COPY web ./web
RUN npm --prefix web run build

FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOTASKS_MODE=cloud \
    DOTASKS_HOST=0.0.0.0 \
    DOTASKS_PORT=8765 \
    DOTASKS_HOME=/data/dotasks

WORKDIR /app

RUN groupadd --system --gid 10001 dotasks \
    && useradd --system --uid 10001 --gid dotasks --home-dir /nonexistent dotasks \
    && mkdir -p /data/dotasks \
    && chown -R dotasks:dotasks /data/dotasks

COPY core ./core
COPY taskboard ./taskboard
COPY --from=web-build /src/static ./static
COPY skills ./skills
COPY scripts/mcp-server scripts/package-cli.py scripts/install-cli scripts/install-online.sh scripts/deploy-cloud-ip ./scripts/
RUN python -B scripts/package-cli.py \
    && mkdir -p static/downloads/cli \
    && cp -R dist/cli/. static/downloads/cli/ \
    && cp scripts/install-online.sh static/install.sh \
    && rm -rf dist

USER dotasks
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,socket; s=socket.create_connection(('127.0.0.1', int(os.environ.get('DOTASKS_PORT','8765'))), 2); s.close()"]

CMD ["python", "-B", "-m", "taskboard.cloud.server"]
