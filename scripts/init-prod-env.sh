#!/usr/bin/env bash
# Generate the production-shaped stack's secrets file (infrastructure/compose.production.yml):
# every ARKRAY_PROD_* secret config/credentials.py lists, each a fresh random value, in a
# file only its owner can read, OUTSIDE the repository. Compose reads it with --env-file,
# never the repository's .env (adversarial review of 73ff1e3, M4: that .env, copied from
# .env.example, once supplied the database superuser's password).
#
# Usage: scripts/init-prod-env.sh /secure/arkray-prod.env
#        scripts/prod-compose.sh /secure/arkray-prod.env up -d --build
#
# Refuses to overwrite an existing file (rotating a secret is docs/runbooks.md#rotate-a-secret,
# not a new file: the database keeps the passwords it was given first) and a path inside
# this checkout. Prints no secret. Needs openssl.
set -euo pipefail
umask 077

if [[ $# -ne 1 || $1 == -* ]]; then
  sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2
  exit 2
fi
target=$1
repo="$(cd "$(dirname "$0")/.." && pwd -P)"

if [[ -e $target ]]; then
  echo "$target already exists; leaving it unchanged." >&2
  exit 1
fi
directory="$(cd "$(dirname "$target")" 2> /dev/null && pwd -P)" || {
  echo "$(dirname "$target") doesn't exist: create it first (owner-only, outside the repository)." >&2
  exit 1
}
case "$directory/" in
  "$repo"/*)
    echo "$target is inside the repository: keep production secrets outside it." >&2
    exit 1
    ;;
esac
command -v openssl > /dev/null 2>&1 || { echo "openssl is required." >&2; exit 1; }

# 32 random bytes, hex: 64 characters, safe inside a connection URL, past every minimum.
generate() { openssl rand -hex 32; }

{
  echo "# Arkray CRM production-shaped stack: secrets for ONE environment, generated $(date -u +%Y-%m-%dT%H:%M:%SZ)."
  echo "# Owner-only, outside the repository, never committed. Read by scripts/prod-compose.sh, never sourced."
  echo "# The database keeps the passwords it was given first: rotate by docs/runbooks.md#rotate-a-secret."
  for name in ARKRAY_PROD_DB_SUPERUSER_PASSWORD ARKRAY_PROD_DB_OWNER_PASSWORD \
    ARKRAY_PROD_DB_APP_PASSWORD ARKRAY_PROD_REDIS_PASSWORD ARKRAY_PROD_DJANGO_SECRET_KEY \
    ARKRAY_PROD_METRICS_TOKEN ARKRAY_PROD_ERASURE_LEDGER_KEY; do
    printf '%s=%s\n' "$name" "$(generate)"
  done
  echo "# Only with AI_LLM_PROVIDER=anthropic and a data-processing agreement (R68); empty: none."
  echo "ARKRAY_PROD_ANTHROPIC_API_KEY="
} > "$target"
chmod 600 "$target"
echo "Created $target (owner-only) with freshly generated production secrets."
