# syntax=docker/dockerfile:1
# Arkray CRM backend image: web (gunicorn), worker and beat all run from this image.

FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# --- build: resolve locked dependencies into a self-contained virtualenv ---------------
FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .

# --- runtime: no build tools, non-root -------------------------------------------------
FROM base AS runtime
RUN groupadd --system --gid 10001 arkray \
 && useradd --system --uid 10001 --gid arkray --home-dir /app --shell /usr/sbin/nologin arkray
COPY --from=build /opt/venv /opt/venv
COPY --from=build --chown=arkray:arkray /app /app
ENV PATH="/opt/venv/bin:${PATH}" \
    DJANGO_SETTINGS_MODULE=config.settings.production
WORKDIR /app
USER arkray
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--config", "gunicorn.conf.py"]
