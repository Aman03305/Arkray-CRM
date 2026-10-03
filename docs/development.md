# Local development

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker Desktop | recent, with Compose v2 | runs PostgreSQL, Redis, Mailpit |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.5 | manages Python 3.12 and the backend virtualenv |
| Node.js | **24 LTS** (≥ 22.12) | Node 20 is end-of-life and too old for the test tooling |
| pnpm | 10 | `corepack enable` |
| Git Bash (Windows) | | for `scripts/*.sh` |

> **Windows / OneDrive:** keep the repository **outside** OneDrive-synced folders (for example
> `C:\dev\arkray-crm`). Syncing `node_modules`, `.venv` and `.next` is slow, can lock
> files mid-build, and uploads thousands of generated files. If you must stay in OneDrive, mark
> those folders "Always keep on this device" or exclude them.

## First-time setup

```bash
scripts/init-env.sh              # .env from .env.example + a generated DJANGO_SECRET_KEY
docker compose up -d             # postgres, redis, mailpit (localhost-only ports)

cd backend
uv sync                          # creates backend/.venv (Python 3.12)
uv run python manage.py migrate
uv run python manage.py createsuperuser   # first admin: email, first/last name, password
uv run python manage.py runserver         # http://localhost:8000/health/ready → {"status": "ok"}

cd ../frontend
pnpm install
pnpm dev                         # http://localhost:3000 (proxies /api → :8000)
```

Background workers deliver invitation and password-reset emails (open Mailpit at
http://127.0.0.1:58025 to read them). On Windows, Celery needs the solo pool:

```bash
cd backend
uv run celery -A config worker -Q outbox,default,email,ai --pool solo --loglevel INFO
uv run celery -A config beat --loglevel INFO
```

Full containerised stack (production settings, Linux containers):

```bash
docker compose --profile app up -d --build   # web :3000, api :8000
```

## Ports

All bind to `127.0.0.1`; change them in `.env` if they collide with other local stacks.

| Service | Host port |
|---|---|
| Next.js | 3000 (`WEB_HOST_PORT`) |
| Django | 8000 (`API_HOST_PORT`) |
| PostgreSQL | 55432 (`POSTGRES_HOST_PORT`) |
| Redis | 56379 (`REDIS_HOST_PORT`) |
| Mailpit UI / SMTP | 58025 / 51025 |

The Compose project is named `arkray`, so its containers, volumes and network never
collide with other projects on the same machine.

## Everyday commands

```bash
scripts/check.sh                          # every quality gate, backend + frontend

# backend (from backend/)
uv run pytest                             # requires `docker compose up -d`
uv run pytest arkray/core -k outbox       # subset
uv run ruff check . && uv run ruff format .
uv run mypy .
uv run lint-imports
uv run python manage.py makemigrations <module>
uv run python manage.py spectacular --file openapi.yaml   # after API changes, then:
# (frontend) pnpm api:types                            # regenerate src/lib/api/schema.gen.ts

# frontend (from frontend/)
pnpm test        # vitest
pnpm lint
pnpm typecheck   # next typegen + tsc
pnpm build
```

## Conventions

- **Module layout:** `models.py`, `selectors.py` (reads, take an `AccessScope`),
  `services.py` (writes, transactions, audit, outbox), `api/`, `handlers.py`, `tests/`
  ([architecture.md](architecture.md#inside-a-module)).
- **New module:** add it to `INSTALLED_APPS` in layer order *and* to the import-linter layer
  list in `backend/pyproject.toml`.
- **New endpoint:** follow the checklist in [api-conventions.md](api-conventions.md#checklist-for-a-new-endpoint).
- **Money:** `DecimalField` only. **Time:** timezone-aware datetimes only (ruff `DTZ`
  enforces this).
- **Side effects** (email, AI, anything slow or external) go through `outbox.enqueue()`
  inside the service's transaction, never inline.
- **Frontend:** module views live in `src/features/<module>`, get their workspace from
  `useWorkspace()`, and call the API only through `apiFetch()`.
- Every architectural decision that future engineers would otherwise have to rediscover gets
  an ADR in `docs/adr/`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `port is already allocated` on `docker compose up` | change the `*_HOST_PORT` values in `.env` |
| Backend tests can't connect | `docker compose up -d postgres`; tests use `localhost:55432` |
| API 404 returns an HTML page locally | expected with `DEBUG=True` (Django's debug page); tests and production return the JSON envelope |
| `markAsUncloneable is not a function` from Vitest | Node is too old; use Node 24 LTS |
| Containers exit with "DJANGO_SECRET_KEY must be a strong, production-only secret" | the app profile uses production settings; run `scripts/init-env.sh` (or put a strong key in `.env`) |
| `python3` fails on Windows | it's the Microsoft Store stub; use `uv run python` |
| Every new DB connection takes ~5 s on Windows | `localhost` resolves to `::1` first, and Docker publishes on 127.0.0.1 only. Use `127.0.0.1` in `DATABASE_URL`, Redis and SMTP URLs (`.env.example` does). |
| `backend/openapi.yaml is stale` / `api:check` fails | regenerate the schema (above), then `pnpm api:types`, and commit both |
