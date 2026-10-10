# syntax=docker/dockerfile:1
# Arkray CRM backend image: web (gunicorn), worker and beat all run from this image.

# Base images are pinned by digest (the tag names the series; Dependabot/renovate or a
# deliberate edit moves the digest): a rebuild never silently changes the base.
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# --- build: resolve locked dependencies into a self-contained virtualenv ---------------
FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.11@sha256:77280f2f771df71f90786c314fe1bbc1e023feac652969bbf139c280babf2eb7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .

# --- model: Ask Arkray's embedding model (BAAI/bge-small-en-v1.5, MIT) -------------------
# Fetched at build time and verified against pinned SHA-256 digests; the running containers
# never download anything. Its own stage, depending only on the (standard-library) fetch
# script: a code change reuses the cached model instead of downloading 130 MB again.
FROM base AS model
COPY arkray/ai/model_files.py /tmp/model_files.py
RUN python /tmp/model_files.py /opt/models/bge-small-en-v1.5

# --- runtime: no build tools, non-root -------------------------------------------------
FROM base AS runtime
# OS security updates published since the base image was built (Phase 9 review).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 arkray \
 && useradd --system --uid 10001 --gid arkray --home-dir /app --shell /usr/sbin/nologin arkray \
 # The writable paths, volumes owned by the runtime user (an empty named volume takes this
 # ownership when first mounted), never served: note attachments (ATTACHMENT_ROOT),
 # data-subject exports (EXPORT_ROOT) and the erasure ledger (ERASURE_LEDGER_URL).
 && install -d -o arkray -g arkray -m 0700 /var/lib/arkray/attachments \
    /var/lib/arkray/exports /var/lib/arkray/ledger
COPY --from=build /opt/venv /opt/venv
# Owned by root, read-only to the runtime user: a file-write bug can't become persistent
# code execution through a recycled worker importing modified code (Phase 9 review).
COPY --from=build /app /app
COPY --from=model /opt/models /opt/models
ENV PATH="/opt/venv/bin:${PATH}" \
    DJANGO_SETTINGS_MODULE=config.settings.production \
    AI_EMBEDDING_MODEL_DIR=/opt/models/bge-small-en-v1.5
WORKDIR /app
USER arkray
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--config", "gunicorn.conf.py"]
