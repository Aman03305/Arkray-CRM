# Arkray CRM — backend

Django 5.2 LTS + Django REST Framework modular monolith. See the repository-root
[README](../README.md) for setup and [docs/architecture.md](../docs/architecture.md) for the
design.

```
config/            settings (base/local/test/production), URLs, Celery app, WSGI
arkray/core/       shared kernel: base models, AccessScope, outbox, logging, errors, health
arkray/audit/      append-only audit trail
arkray/identity/   users, roles -> capabilities, workspace resolution
tests/             cross-module tests: architecture guards, integration, authz matrix
```

Common commands (from `backend/`):

```bash
uv sync                                   # install (creates .venv)
uv run python manage.py migrate
uv run python manage.py runserver         # http://localhost:8000/health/ready
uv run pytest                             # needs `docker compose up -d` (PostgreSQL)
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run lint-imports                       # module boundary contracts
uv run python manage.py spectacular --file schema.yaml   # OpenAPI schema
```
