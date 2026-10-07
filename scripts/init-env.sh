#!/usr/bin/env bash
# Create a local .env from .env.example with a freshly generated DJANGO_SECRET_KEY.
# Refuses to overwrite an existing .env. Usage: scripts/init-env.sh
set -euo pipefail
umask 077  # .env holds the secret key and database password: owner-only

cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  echo ".env already exists; leaving it unchanged." >&2
  exit 0
fi

generate_key() {
  # `python3` can be a non-functional Microsoft Store stub on Windows, so probe each one.
  for bin in python3 python py; do
    if command -v "$bin" >/dev/null 2>&1 && "$bin" -c "import secrets" >/dev/null 2>&1; then
      "$bin" -c "import secrets; print(secrets.token_urlsafe(64))"
      return
    fi
  done
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 64 | tr -d '\n=+/'
    echo
    return
  fi
  echo "Python or OpenSSL is required to generate a secret key." >&2
  exit 1
}

key="$(generate_key)"

# Replace the placeholder key line; everything else is copied verbatim.
while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" == DJANGO_SECRET_KEY=* ]]; then
    printf 'DJANGO_SECRET_KEY=%s\n' "$key"
  else
    printf '%s\n' "$line"
  fi
done < .env.example > .env

echo "Created .env with a generated DJANGO_SECRET_KEY."
