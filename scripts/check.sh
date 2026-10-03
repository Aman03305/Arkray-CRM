#!/usr/bin/env bash
# Run every quality gate for backend and frontend. Mirrors CI (.github/workflows/ci.yml).
# Requires the local PostgreSQL from `docker compose up -d` for backend tests.
#
# Usage: scripts/check.sh            # everything
#        scripts/check.sh backend    # one side only
#        scripts/check.sh frontend
set -euo pipefail

cd "$(dirname "$0")/.."
target="${1:-all}"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

check_backend() {
  cd backend
  step "backend: ruff lint";          uv run ruff check .
  step "backend: ruff format";        uv run ruff format --check .
  step "backend: mypy (strict)";      uv run mypy .
  step "backend: module boundaries";  uv run lint-imports
  step "backend: migrations in sync"; uv run python manage.py makemigrations --check --dry-run
  step "backend: tests";              uv run pytest --cov --cov-report=term-missing:skip-covered
  step "backend: dependency audit"
  uv export --frozen --no-dev --no-hashes --format requirements-txt > "${TMPDIR:-/tmp}/arkray-requirements.txt"
  uv run pip-audit --progress-spinner off -r "${TMPDIR:-/tmp}/arkray-requirements.txt"
  cd ..
}

check_frontend() {
  local major
  major="$(node -p 'process.versions.node.split(".")[0]')"
  if (( major < 22 )); then
    echo "Node >= 22.12 is required (found $(node --version)). Install Node 24 LTS." >&2
    exit 1
  fi
  cd frontend
  step "frontend: install";     pnpm install --frozen-lockfile
  step "frontend: API types match backend/openapi.yaml"; pnpm api:check
  step "frontend: eslint";      pnpm lint
  step "frontend: typecheck";   pnpm typecheck
  step "frontend: tests";       pnpm test
  step "frontend: build";       pnpm build
  step "frontend: audit";       pnpm audit --prod --audit-level high
  cd ..
}

case "$target" in
  backend)  check_backend ;;
  frontend) check_frontend ;;
  all)      check_backend; check_frontend ;;
  *) echo "usage: $0 [backend|frontend|all]" >&2; exit 2 ;;
esac

step "all checks passed"
