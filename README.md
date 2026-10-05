# Arkray CRM

A sales CRM built from scratch as a **modular monolith**: Django 5.2 LTS + DRF +
PostgreSQL 16 (pgvector) + Redis + Celery on the backend, Next.js 16 + TypeScript on the
frontend.

Normal users work in three modules — **Dashboard, Pipeline, Activities**. A new opportunity
creates its lead (the canonical customer record) in the same transaction; leads are shown
read-only (their page, the dashboard's new leads, search) but there is no Leads module
([ADR-0027](docs/adr/0027-leads-removed-from-the-ui.md), [ADR-0028](docs/adr/0028-opportunity-creates-its-lead.md)). Admins
additionally manage **Users** and can open any user's CRM workspace
(`/admin/users/{id}/dashboard`), with every such access authorised and audited server-side.
**Ask Arkray** answers questions about CRM data strictly within the asker's permissions.

> Status: **all eleven phases built** (identity and users, leads, pipeline, activities,
> dashboards, the admin user workspace, global search, Ask Arkray, security hardening,
> performance and reliability, production readiness). The release candidate awaits
> approval; see [docs/architecture.md](docs/architecture.md#delivery-phases) for the phase
> plan and [docs/testing.md](docs/testing.md) for what each phase verified.

## Repository layout

```
backend/          Django API (config/, arkray/<module>/, tests/)
frontend/         Next.js app (src/app, src/components, src/features, src/lib)
infrastructure/   the edge proxy, database roles, a production-shaped Compose stack
docs/             architecture, database, authorization, RAG, security, ... + ADRs
scripts/          init-env.sh (create .env), check.sh (all quality gates),
                  backup.sh, restore.sh
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
| Leads (the canonical customer record; read-only in the UI, ADR-0028): model, ownership, statuses, search, concurrency | [leads.md](docs/leads.md) |
| Pipeline: stages, opportunities, transitions, conversion, money, lock order | [pipeline.md](docs/pipeline.md) |
| Activities: tasks, meetings, notes, the timeline | [activities.md](docs/activities.md) |
| Schema, ERD, constraints, indexes | [database.md](docs/database.md) |
| Authentication, authorization, admin workspace | [authorization.md](docs/authorization.md) |
| Ask Arkray (RAG) | [rag-architecture.md](docs/rag-architecture.md) |
| Threat model and controls | [security.md](docs/security.md) |
| Async processing, failure & degradation | [reliability.md](docs/reliability.md) |
| Logging, health, metrics, tracing | [observability.md](docs/observability.md) |
| API conventions | [api-conventions.md](docs/api-conventions.md) |
| Test strategy | [testing.md](docs/testing.md) |
| Architectural risks and their resolutions | [risk-register.md](docs/risk-register.md) |
| Global search | [search.md](docs/search.md) |
| Dashboards, admin user workspace | [dashboard.md](docs/dashboard.md), [admin-user-workspace.md](docs/admin-user-workspace.md) |
| Deployment: topology, proxy, configuration, database, backups | [deployment.md](docs/deployment.md) |
| Go-live and release checklist | [production-checklist.md](docs/production-checklist.md) |
| Runbooks: alerts, restores, secret rotation, erasure | [runbooks.md](docs/runbooks.md) |
| Personal data, retention, access and erasure requests | [privacy.md](docs/privacy.md) |
| Operations: the RAG index, the embedding model | [operations.md](docs/operations.md) |
| Local development | [development.md](docs/development.md) |
| Decision records | [docs/adr/](docs/adr/) |
