# Infrastructure

The production artifacts, verified together in Phase 11. The rules they implement, and why,
are in [docs/deployment.md](../docs/deployment.md); the procedures that use them are in
[docs/runbooks.md](../docs/runbooks.md).

| File | What it is |
|---|---|
| `nginx/arkray.conf` | the reference edge proxy: TLS and HSTS, `/api` to Django and the rest to Next.js, overwritten forwarding headers, a minted request id, an access log without query strings or one-time links, `/health` not routed ([deployment.md](../docs/deployment.md#reverse-proxy)) |
| `postgres/roles.sql` | once per environment, as a database administrator: the database, `arkray_owner` and `arkray_app`, the extensions, R63's logging setting ([deployment.md](../docs/deployment.md#database)) |
| `alerts/arkray.rules.yml` | Prometheus alerting rules over `/health/metrics`: the metric-based alerts of [observability.md](../docs/observability.md#alerts-phase-10), each linking its runbook (whole-software audit) |
| `compose.production.yml` | a production-shaped stack on one machine (with the root `docker-compose.yml`): HTTPS at the reference proxy, both database roles, Redis with a password, read-only containers, no insecure opt-in. The release is verified on it, and it is the reference for real manifests ([deployment.md](../docs/deployment.md#verifying-a-release)) |

Backups and restores: `scripts/backup.sh`, `scripts/restore.sh`
([deployment.md](../docs/deployment.md#backup-restore-and-disaster-recovery)).
Local development infrastructure is the repository-root `docker-compose.yml`.
