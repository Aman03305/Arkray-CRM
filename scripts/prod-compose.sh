#!/usr/bin/env bash
# docker compose for the production-shaped stack (infrastructure/compose.production.yml), with
# its secrets file as the ONLY source of its secrets (review round 1 of the adversarial review
# of 73ff1e3, INFRA-5).
#
# Usage: scripts/prod-compose.sh <secrets file> [--project NAME] <compose command and options>
#        scripts/prod-compose.sh /secure/arkray-prod.env up -d --build
#        scripts/prod-compose.sh /secure/arkray-prod.env ps
#        scripts/prod-compose.sh /secure/arkray-stage.env --project arkray-stage logs migrate
#
# Compose gives a variable exported in the shell precedence over --env-file: an
# ARKRAY_PROD_* left in the shell (an earlier `set -a; . file`, another environment's file)
# would silently replace the file's value, and the guard can't tell a stale value from a
# fresh one. So this unsets every ARKRAY_PROD_* (and COMPOSE_ENV_FILES, which adds env
# files) in its own process, then runs `docker compose --env-file <file> -p <project>
# -f docker-compose.yml -f infrastructure/compose.production.yml --profile app <command>`
# from the checkout's root. The project is arkray-prod unless --project names another. It
# refuses a file that isn't there and one inside the checkout (secrets stay out of the
# repository; scripts/init-prod-env.sh writes one). Prints nothing of its own.
set -euo pipefail

usage() {
  sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2
  exit 2
}

[[ $# -ge 2 && $1 != -* ]] || usage
secrets=$1
shift
project=arkray-prod
if [[ $1 == --project ]]; then
  [[ $# -ge 3 ]] || usage
  project=$2
  shift 2
fi

repo="$(cd "$(dirname "$0")/.." && pwd -P)"
[[ -f $secrets && -r $secrets ]] || { echo "$secrets isn't a readable file." >&2; exit 1; }
directory="$(cd "$(dirname "$secrets")" && pwd -P)"
case "$directory/" in
  "$repo"/*)
    echo "$secrets is inside the repository: keep production secrets outside it." >&2
    exit 1
    ;;
esac
secrets="$directory/$(basename "$secrets")"
# Git Bash on Windows: docker.exe wants C:/... (as scripts/ci-local.sh's host_path).
if command -v cygpath > /dev/null 2>&1; then secrets="$(cygpath -m "$secrets")"; fi

while IFS= read -r name; do
  unset "$name"
done < <(compgen -e | grep -E '^(ARKRAY_PROD_[A-Z0-9_]*|COMPOSE_ENV_FILES)$' || true)

cd "$repo"
exec docker compose --env-file "$secrets" -p "$project" \
  -f docker-compose.yml -f infrastructure/compose.production.yml --profile app "$@"
