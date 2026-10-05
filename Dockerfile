# syntax=docker/dockerfile:1
FROM python:3.14.7-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages/research_engine/pyproject.toml,target=packages/research_engine/pyproject.toml \
    --mount=type=bind,source=packages/research_engine_client/pyproject.toml,target=packages/research_engine_client/pyproject.toml \
    uv sync --locked --no-install-workspace --package research-engine
COPY packages ./packages
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --package research-engine --no-editable

FROM python:3.14.7-slim-trixie
ARG GIT_SHA=unknown
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown 10001:10001 /data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY config ./config
COPY schemas ./schemas
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 DB_PATH=/data/research-engine.sqlite GIT_SHA=${GIT_SHA}
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
CMD ["uvicorn", "research_engine.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "5"]
