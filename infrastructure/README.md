# Infrastructure

Deployment topology, configuration and operational rules are defined in
[docs/deployment.md](../docs/deployment.md). This directory will hold the concrete,
environment-specific artifacts, added in Phase 11 once the hosting platform is chosen:

- reverse-proxy configuration (TLS, HSTS, routing `/api` and `/health` → Django, `/` → Next.js)
- container orchestration manifests (api, web, workers per queue, a single beat, migrate job)
- PostgreSQL role and privilege scripts (`arkray_owner` / `arkray_app`, append-only grants)
- PgBouncer configuration
- backup, restore and disaster-recovery runbooks

Local development infrastructure is the repository-root `docker-compose.yml`.
