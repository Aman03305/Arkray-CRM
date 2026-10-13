#!/usr/bin/env bash
# Run CI (.github/workflows/ci.yml) on this machine, job by job, as CI runs it: the same
# pinned tools (gitleaks, actionlint, Trivy and PostgreSQL images by digest), the same gates,
# and the images' smoke test with exactly CI's production environment.
# tests/architecture/test_ci_workflow.py fails when this script and the workflow disagree
# (pins, environment, hardening, the fresh-database release).
#
# Usage: scripts/ci-local.sh                        # every job, in CI's order
#        scripts/ci-local.sh --only images          # one job; repeat --only for more
#        scripts/ci-local.sh --only images --ref HEAD
#                                                   # the images from a commit, as CI builds them
#
# Jobs:
#   secrets    gitleaks over every commit (CI's scan), then over what CI will scan once it
#              is committed: changes staged and not, and untracked files that aren't ignored
#   workflows  actionlint, with shellcheck on every `run:` script
#   compose    the production-shaped stack refuses the development environment file for each
#              secret it requires (CI copies .env.example to .env; here it is given with
#              --env-file, so your own .env is never touched or read)
#   backend    uv.lock against pyproject.toml, the embedding model, then
#              `scripts/check.sh backend` (needs the local PostgreSQL; DATABASE_URL is passed
#              through: give your own database name when others run tests too)
#   frontend   `scripts/check.sh frontend` (Node >= 22.12 on PATH)
#   images     both builds, Trivy's three gates per image, the hardened smoke test, a
#              release on a new database (roles.sql, migrate as the owner, migrate --check,
#              the release check as the application role), and the credential guard
# Run in full, images is skipped when backend or frontend failed (CI's `needs`).
#
# The images are built from the working tree unless --ref names a commit (exported with
# `git archive`: a clean checkout, as CI builds). Smoke-test ports: CI_LOCAL_API_PORT (18000)
# and CI_LOCAL_WEB_PORT (13000) on 127.0.0.1. Works in Git Bash on Windows and on Linux; needs
# git, docker and openssl. Prints no secret: gitleaks runs with --redact, the smoke test's keys
# and passwords are random, never printed, and reach the database containers through the
# environment.
set -uo pipefail

# Docker Desktop on Windows: paths go to docker.exe unconverted (host_path converts them).
export MSYS_NO_PATHCONV=1

cd "$(dirname "$0")/.." || exit 2
repo="$PWD"

# Pinned exactly as in .github/workflows/ci.yml (the architecture test compares them).
GITLEAKS_IMAGE=ghcr.io/gitleaks/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
ACTIONLINT_IMAGE=rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667
TRIVY_IMAGE=aquasec/trivy:0.70.0@sha256:be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e
POSTGRES_IMAGE=pgvector/pgvector:0.8.7-pg16-trixie@sha256:40d828eda66737aaacb934387a337babf1455ebd56e9bdd13fc08e211257c972

# Names of their own: never the development stack's or another run's containers.
BACKEND_IMAGE=arkray-backend:ci-local
FRONTEND_IMAGE=arkray-frontend:ci-local
API=arkray-ci-local-api
WEB=arkray-ci-local-web
DB=arkray-ci-local-db
TRIVY_CACHE=arkray-ci-local-trivy-cache
API_PORT="${CI_LOCAL_API_PORT:-18000}"
WEB_PORT="${CI_LOCAL_WEB_PORT:-13000}"
WORKDIR="${CI_LOCAL_WORKDIR:-${TMPDIR:-/tmp}/arkray-ci-local}"

JOBS=(secrets workflows compose backend frontend images)

usage() {
  sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//'
  exit "${1:-0}"
}

only=()
ref=""
while (($#)); do
  case "$1" in
    --only)
      [[ $# -ge 2 ]] || usage 2
      IFS=, read -r -a names <<< "$2"
      for name in "${names[@]}"; do
        [[ " ${JOBS[*]} " == *" $name "* ]] || { echo "unknown job: $name (${JOBS[*]})" >&2; exit 2; }
        only+=("$name")
      done
      shift 2
      ;;
    --ref)
      [[ $# -ge 2 ]] || usage 2
      ref="$2"
      shift 2
      ;;
    -h | --help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
done
((${#only[@]})) || only=("${JOBS[@]}")

selected() { [[ " ${only[*]} " == *" $1 "* ]]; }

# A path docker.exe understands (C:/... on Windows); unchanged elsewhere.
host_path() {
  if command -v cygpath > /dev/null 2>&1; then cygpath -m "$1"; else printf '%s\n' "$1"; fi
}

results=()
gate() {
  local name=$1 started=$SECONDS status
  shift
  printf '\n\033[1m== %s\033[0m\n' "$name"
  if "$@"; then status=PASS; else status=FAIL; fi
  results+=("$status|$name|$((SECONDS - started))")
  [[ $status == PASS ]]
}
skip() {
  results+=("SKIP|$1 ($2)|0")
  printf '\n\033[1m== %s\033[0m: skipped (%s)\n' "$1" "$2"
}

cleanup() { docker rm -f "$API" "$WEB" "$DB" > /dev/null 2>&1 || true; }
trap cleanup EXIT
trap 'exit 130' INT TERM

# --- secrets ----------------------------------------------------------------------------
gitleaks() {
  docker run --rm -v "$(host_path "$repo"):/repo:ro" "$GITLEAKS_IMAGE" git --redact --verbose --no-banner --exit-code 1 "$@" /repo
}
# New files git doesn't track yet (ignored ones, such as .env, stay out), copied aside and
# scanned as a directory: CI scans them once they are committed.
untracked_files() {
  local dir="$WORKDIR/untracked"
  rm -rf "$dir" && mkdir -p "$dir" || return 1
  git ls-files --others --exclude-standard -z | tar --null -T - -cf - | tar -xf - -C "$dir" || return 1
  echo "$(find "$dir" -type f | wc -l | tr -d ' ') untracked file(s)"
  local status=0
  docker run --rm -v "$(host_path "$dir"):/scan:ro" "$GITLEAKS_IMAGE" dir --redact --verbose --no-banner --exit-code 1 /scan ||
    status=1
  rm -rf "$dir"
  return "$status"
}
job_secrets() {
  local failed=0
  gate "secrets: every commit (CI's scan)" gitleaks || failed=1
  gate "secrets: uncommitted changes, not staged" gitleaks --pre-commit || failed=1
  gate "secrets: uncommitted changes, staged" gitleaks --staged || failed=1
  gate "secrets: untracked files" untracked_files || failed=1
  return "$failed"
}

# --- workflows --------------------------------------------------------------------------
job_workflows() {
  gate "workflows: actionlint and shellcheck" \
    docker run --rm -v "$(host_path "$repo"):/repo:ro" -w /repo "$ACTIONLINT_IMAGE"
}

# --- compose ----------------------------------------------------------------------------
# CI's check (.github/workflows/ci.yml, job compose): every secret the production overlay
# requires given but one, with the development environment file; Compose must refuse,
# naming it. .env.example is given with --env-file (CI copies it to .env): the same values,
# and this checkout's own .env stays untouched.
compose_configure() {
  local name value
  for name in "${compose_required[@]}"; do
    value="$(openssl rand -hex 32)"
    if [[ $name == "$1" ]]; then unset "$name"; else export "$name=$value"; fi
  done
  local files=(-p arkray-ci-local-production -f docker-compose.yml -f infrastructure/compose.production.yml --profile app)
  docker compose --env-file .env.example "${files[@]}" config --quiet
}
compose_refuses_development_credentials() {
  local missing errors="$WORKDIR/compose.err"
  mkdir -p "$WORKDIR" || return 1
  mapfile -t compose_required < <(grep -v '^ *#' infrastructure/compose.production.yml |
    grep -oE '\$\{[A-Z][A-Z0-9_]*:\?' | sed -E 's/^\$\{//; s/:\?$//' | sort -u)
  ((${#compose_required[@]} >= 7)) || { echo "the overlay requires only ${#compose_required[@]} secrets" >&2; return 1; }
  for missing in "${compose_required[@]}"; do
    if (compose_configure "$missing") 2> "$errors"; then
      echo "the production stack took $missing from the development environment file" >&2
      return 1
    fi
    grep -q "$missing" "$errors" || { cat "$errors" >&2; return 1; }
  done
  (compose_configure "") || return 1
  echo "refused for each of ${#compose_required[@]} production secrets: ${compose_required[*]}"
}
job_compose() {
  gate "compose: the production stack refuses development credentials" compose_refuses_development_credentials
}

# --- backend and frontend ---------------------------------------------------------------
# CI's backend job runs `uv sync --locked` and `ai_fetch_model` before the gates that
# scripts/check.sh runs; the lock check here changes nothing in the shared environment.
backend_lock() { (cd backend && uv lock --check); }
backend_model() { (cd backend && DJANGO_SETTINGS_MODULE=config.settings.test uv run python manage.py ai_fetch_model); }
job_backend() {
  local failed=0
  gate "backend: uv.lock matches pyproject.toml" backend_lock || failed=1
  gate "backend: the pinned embedding model" backend_model || failed=1
  gate "backend: scripts/check.sh backend" env DJANGO_SETTINGS_MODULE=config.settings.test scripts/check.sh backend ||
    failed=1
  return "$failed"
}
job_frontend() { gate "frontend: scripts/check.sh frontend" scripts/check.sh frontend; }

# --- images -----------------------------------------------------------------------------
source_tree() {
  if [[ -z $ref ]]; then
    printf '%s\n' "$repo"
    return
  fi
  local sha dir
  sha="$(git rev-parse --verify --quiet "$ref^{commit}")" || { echo "not a commit: $ref" >&2; return 1; }
  dir="$WORKDIR/src-$sha"
  rm -rf "$dir" && mkdir -p "$dir" || return 1
  git archive --format=tar "$sha" backend frontend infrastructure/postgres | tar -x -C "$dir" || return 1
  printf '%s\n' "$dir"
}

trivy() {
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$TRIVY_CACHE:/root/.cache" \
    "$TRIVY_IMAGE" image --skip-version-check "$@"
}

smoke() {
  local api_url="http://localhost:$API_PORT" web_url="http://localhost:$WEB_PORT"
  # Host: localhost, as CI's requests (DJANGO_ALLOWED_HOSTS=localhost), over IPv4: Windows
  # tries ::1 first for localhost, where Docker Desktop doesn't forward.
  local to_local=(--resolve "localhost:$API_PORT:127.0.0.1" --resolve "localhost:$WEB_PORT:127.0.0.1")
  docker run --rm "${prod_env[@]}" "${hardened[@]}" "$BACKEND_IMAGE" python manage.py check --deploy || return 1
  docker run -d --name "$API" "${prod_env[@]}" "${hardened[@]}" -p "127.0.0.1:$API_PORT:8000" "$BACKEND_IMAGE" > /dev/null ||
    return 1
  docker run -d --name "$WEB" "${hardened[@]}" --tmpfs /app/.next/cache:uid=10001,gid=10001 \
    -p "127.0.0.1:$WEB_PORT:3000" "$FRONTEND_IMAGE" > /dev/null || return 1
  for _ in $(seq 1 30); do curl -fsS "${to_local[@]}" "$api_url/health/live" > /dev/null 2>&1 && break; sleep 2; done
  curl -fsS "${to_local[@]}" "$api_url/health/live" || return 1
  echo
  curl -fsS "${to_local[@]}" "$web_url/login" > /dev/null || return 1
  echo "web: /login answered"
  docker logs "$API" 2>&1 | tail -20
  # A crash prints a traceback; structured lines carry their level.
  if docker logs "$API" 2>&1 | grep -E '\[ERROR\]|Traceback|Read-only file system|"level": "(ERROR|CRITICAL)"'; then
    echo "the API logged errors" >&2
    return 1
  fi
}

# docs/deployment.md#release-process, steps 2-4, on a new database: CI's last smoke-test lines.
fresh_database() {
  local src=$1
  local -x POSTGRES_PASSWORD OWNER_PASSWORD APP_PASSWORD
  POSTGRES_PASSWORD="$(openssl rand -hex 24)"
  OWNER_PASSWORD="$(openssl rand -hex 24)"
  APP_PASSWORD="$(openssl rand -hex 24)"
  docker rm -f "$DB" > /dev/null 2>&1
  docker run -d --name "$DB" -e POSTGRES_USER=arkray -e POSTGRES_DB=postgres -e POSTGRES_PASSWORD \
    "$POSTGRES_IMAGE" > /dev/null || return 1
  # TCP answers only once the image's first-start initialisation is over.
  for _ in $(seq 1 30); do docker exec "$DB" pg_isready -q -h 127.0.0.1 -U arkray && break; sleep 2; done
  docker exec -i -e OWNER_PASSWORD -e APP_PASSWORD "$DB" \
    psql -q -U arkray -d postgres -v database=arkray -f - < "$src/infrastructure/postgres/roles.sql" || return 1
  local on_db=(--network "container:$DB")
  DATABASE_URL="postgres://arkray_owner:${OWNER_PASSWORD}@127.0.0.1:5432/arkray" \
    docker run --rm "${prod_env[@]}" -e DATABASE_URL "${hardened[@]}" "${on_db[@]}" "$BACKEND_IMAGE" \
    sh -c 'python manage.py migrate --noinput && python manage.py grant_app_privileges arkray_app && python manage.py migrate --check' ||
    return 1
  DATABASE_URL="postgres://arkray_app:${APP_PASSWORD}@127.0.0.1:5432/arkray" \
    docker run --rm "${prod_env[@]}" -e DATABASE_URL "${hardened[@]}" "${on_db[@]}" "$BACKEND_IMAGE" \
    python manage.py check --deploy --database default
}

# CI's guard check (images job): the published development password refused, generated
# secrets accepted, in the image.
credential_guard() {
  local names=(ARKRAY_PROD_DB_SUPERUSER_PASSWORD ARKRAY_PROD_DB_OWNER_PASSWORD ARKRAY_PROD_DB_APP_PASSWORD
    ARKRAY_PROD_REDIS_PASSWORD ARKRAY_PROD_DJANGO_SECRET_KEY ARKRAY_PROD_METRICS_TOKEN ARKRAY_PROD_ERASURE_LEDGER_KEY)
  local guard=(docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true)
  local name value
  for name in "${names[@]}"; do
    value="$(openssl rand -hex 32)"
    local -x "$name=$value"
    guard+=(-e "$name")
  done
  "${guard[@]}" "$BACKEND_IMAGE" python -m config.credentials || return 1
  local -x ARKRAY_PROD_DB_SUPERUSER_PASSWORD=arkray_dev_only_password
  if "${guard[@]}" "$BACKEND_IMAGE" python -m config.credentials; then
    echo "the guard accepted the published development password" >&2
    return 1
  fi
}

job_images() {
  local failed=0 src built=1 image
  src="$(source_tree)" || return 1
  if [[ -z $ref ]]; then
    echo "images: built from the working tree (uncommitted changes included); CI builds the commit (--ref)"
  else
    echo "images: built from $(git rev-parse --short "$ref^{commit}") (git archive)"
  fi
  gate "images: build backend" docker build -t "$BACKEND_IMAGE" "$(host_path "$src/backend")" || { failed=1; built=0; }
  gate "images: build frontend" docker build --build-arg API_ORIGIN=http://backend:8000 -t "$FRONTEND_IMAGE" \
    "$(host_path "$src/frontend")" || { failed=1; built=0; }
  if ((!built)); then
    skip "images: scans, smoke test and fresh database" "a build failed"
    [[ -z $ref ]] || rm -rf "$src"
    return 1
  fi
  for image in "$BACKEND_IMAGE" "$FRONTEND_IMAGE"; do
    gate "images: $image everything HIGH or CRITICAL (informational)" \
      trivy --scanners vuln --severity HIGH,CRITICAL "$image" || failed=1
    gate "images: $image fixable HIGH or CRITICAL (blocking)" \
      trivy --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$image" || failed=1
    gate "images: $image secrets, any severity (blocking)" \
      trivy --scanners secret --exit-code 1 "$image" || failed=1
  done
  # CI's smoke-test environment, exactly.
  prod_env=(
    -e DJANGO_SECRET_KEY="ci-image-smoke-$(openssl rand -hex 32)"
    -e DJANGO_ALLOWED_HOSTS=localhost
    -e DATABASE_URL=postgres://arkray_app:x@127.0.0.1:5432/arkray
    -e CELERY_BROKER_URL=redis://:x@127.0.0.1:6379/0
    -e REDIS_CACHE_URL=redis://:x@127.0.0.1:6379/1
    -e TRUSTED_PROXY_COUNT=1 -e TRUSTED_PROXY_ADDRESSES=10.0.0.10
    -e APP_BASE_URL=https://crm.example.com
    -e EMAIL_URL=smtp+tls://user:x@smtp.example.com:587
    -e ERASURE_LEDGER_URL=file:///tmp/erasure-ledger
    -e ERASURE_LEDGER_KEY="ci-image-smoke-ledger-$(openssl rand -hex 32)"
    -e DEFAULT_FROM_EMAIL=no-reply@crm.example.com
    -e ATTACHMENT_SCANNER_WAIVED=true
  )
  hardened=(--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true)
  cleanup
  gate "images: hardened smoke test" smoke || failed=1
  gate "images: release on a new database (roles.sql, migrate, release check)" fresh_database "$src" ||
    failed=1
  gate "images: the credential guard refuses development values" credential_guard || failed=1
  cleanup
  [[ -z $ref ]] || rm -rf "$src"
  return "$failed"
}

# --- run --------------------------------------------------------------------------------
declare -A outcome=()
for job in "${JOBS[@]}"; do
  selected "$job" || continue
  if [[ $job == images && ${#only[@]} -eq ${#JOBS[@]} ]] &&
    [[ ${outcome[backend]:-} == FAIL || ${outcome[frontend]:-} == FAIL ]]; then
    skip "images" "backend or frontend failed"
    continue
  fi
  if "job_$job"; then outcome[$job]=PASS; else outcome[$job]=FAIL; fi
done

printf '\n\033[1m== ci-local summary\033[0m (%s)\n' "${only[*]}"
failures=0
for result in "${results[@]}"; do
  IFS='|' read -r status name seconds <<< "$result"
  printf '  %-4s  %-78s %5ss\n' "$status" "$name" "$seconds"
  [[ $status != FAIL ]] || failures=$((failures + 1))
done
if ((failures)); then
  printf '\nFAIL: %d of %d gates failed\n' "$failures" "${#results[@]}"
  exit 1
fi
printf '\nPASS: %d gates\n' "${#results[@]}"
