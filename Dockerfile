# One image for the onboarding API and the mock core-banking service (DECISIONS D-11).
# The command selects the process:
#   onboarding API    : uvicorn onboarding.api.main:get_app --factory   (Phase 2/3)
#   mock core banking : uvicorn mock_bank.main:get_app --factory
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv
WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 PATH="/opt/venv/bin:$PATH"
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /opt/venv /opt/venv
WORKDIR /srv
COPY data ./data
COPY prompts ./prompts
ENV ONBOARDING_DATA_DIR=/srv/data ONBOARDING_PROMPTS_DIR=/srv/prompts
# Local-development document store (a named volume copies these permissions); the cluster uses S3.
RUN mkdir -p /data/documents && chown -R 10001:10001 /data
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=3)"]
CMD ["sh", "-c", "exec uvicorn mock_bank.main:get_app --factory --host 0.0.0.0 --port ${PORT}"]
