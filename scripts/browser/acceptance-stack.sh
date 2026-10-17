#!/bin/sh
# The isolated acceptance stack for the browser runs: its own Compose project (arkray-accept), volumes,
# Redis, database and mail catcher, on ports that touch neither the development stack nor anything else
# (database 55532, Redis 56479, SMTP 51125, Mailpit 58225, API 8100, web 3100).
#
#   scripts/browser/acceptance-stack.sh up      build and start it (ACCEPT_MFA=true: Super admins need a 2nd factor)
#   scripts/browser/acceptance-stack.sh ps|logs [service]
#   scripts/browser/acceptance-stack.sh down    remove it and its volumes (this project only)
set -eu
cd "$(dirname "$0")/../.."

export POSTGRES_HOST_PORT=55532 REDIS_HOST_PORT=56479 MAILPIT_SMTP_HOST_PORT=51125 MAILPIT_UI_HOST_PORT=58225
export API_HOST_PORT=8100 WEB_HOST_PORT=3100 AI_ENABLED=true AI_LLM_PROVIDER=none
export APP_BASE_URL=http://localhost:3100
export DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:3100,http://127.0.0.1:3100
compose="docker compose -p arkray-accept -f docker-compose.yml -f scripts/browser/acceptance-overlay.yml --profile app"

case "${1:-}" in
  up) $compose up -d --build ;;
  down) $compose down -v ;;
  ps) $compose ps ;;
  logs) shift; $compose logs --tail 80 "$@" ;;
  *) echo "usage: $0 up|down|ps|logs [service]" >&2; exit 2 ;;
esac
