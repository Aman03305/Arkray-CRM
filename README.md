# Arkray CRM

A sales CRM built from scratch as a **modular monolith**: Django 5.2 LTS + DRF +
PostgreSQL 16 (pgvector) + Redis + Celery on the backend, Next.js 16 + TypeScript on the
frontend.

Normal users work in four modules — **Dashboard, Pipeline, Leads, Activities**. Admins
additionally manage **Users** and can open any user's CRM workspace
(`/admin/users/{id}/dashboard`), with every such access authorised and audited server-side.
**Ask Arkray** answers questions about CRM data strictly within the asker's permissions.

> Status: **Phase 3 — Pipeline** complete (pipelines and configurable stages,
> opportunities owned by their lead's owner, a Kanban board with drag and drop and a
> keyboard Move menu, won/lost/reopen, append-only stage history, lead conversion, exact
> pipeline value and weighted pipeline; see [docs/pipeline.md](docs/pipeline.md)), on top
> of Phase 2 (Leads, [docs/leads.md](docs/leads.md)) and Phase 1 (identity, sessions,
> throttling, invitations, password reset, admin Users area).
> See [docs/architecture.md](docs/architecture.md#delivery-phases) for the phase plan.

## Repository layout

```
backend/          Django API (config/, arkray/<module>/, tests/)
frontend/         Next.js app (src/app, src/components, src/features, src/lib)
infrastructure/   deployment notes and (later) production manifests
docs/             architecture, database, authorization, RAG, security, ... + ADRs
scripts/          init-env.sh (create .env), check.sh (all quality gates)
docker-compose.yml
```

## Quick start (local development)

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), **Node 24 LTS** (≥ 22.12) with
pnpm 10, Git Bash on Windows. Full details: [docs/development.md](docs/development.md).

```bash
scripts/init-env.sh                  # creates .env with a generated secret key
docker compose up -d                 # PostgreSQL :55432, Redis :56379, Mailpit :58025

cd backend
uv sync
uv run python manage.py migrate
uv run python manage.py createsuperuser   # first admin (email, first and last name)
uv run python manage.py runserver         # http://localhost:8000/health/ready

cd ../frontend
pnpm install
pnpm dev                                  # http://localhost:3000
```

Everything in containers instead: `docker compose --profile app up -d --build`.

## Quality gates

```bash
scripts/check.sh      # backend: ruff, mypy (strict), import contracts, pytest, pip-audit
                      # frontend: eslint, tsc, vitest, next build
```

## Documentation

| Topic | Document |
|---|---|
| System design, module boundaries, phases | [architecture.md](docs/architecture.md) |
| Leads: model, ownership, statuses, search, concurrency | [leads.md](docs/leads.md) |
| Pipeline: stages, opportunities, transitions, conversion, money, lock order | [pipeline.md](docs/pipeline.md) |
| Schema, ERD, constraints, indexes | [database.md](docs/database.md) |
| Authentication, authorization, admin workspace | [authorization.md](docs/authorization.md) |
| Ask Arkray (RAG) | [rag-architecture.md](docs/rag-architecture.md) |
| Threat model and controls | [security.md](docs/security.md) |
| Async processing, failure & degradation | [reliability.md](docs/reliability.md) |
| Logging, health, metrics, tracing | [observability.md](docs/observability.md) |
| API conventions | [api-conventions.md](docs/api-conventions.md) |
| Test strategy | [testing.md](docs/testing.md) |
| Architectural risks and their resolutions | [risk-register.md](docs/risk-register.md) |
| Deployment | [deployment.md](docs/deployment.md) |
| Local development | [development.md](docs/development.md) |
| Decision records | [docs/adr/](docs/adr/) |
