#!/usr/bin/env bash
# Logical backup of the Arkray database (docs/runbooks.md#restore-from-backup).
#
#   DATABASE_URL=postgres://user:pass@host:5432/arkray scripts/backup.sh [output-dir]
#
# Writes arkray-<UTC timestamp>.dump (pg_dump custom format, compressed) and a .sha256 next
# to it, after checking that the archive reads back. Managed PostgreSQL's point-in-time
# recovery is the primary backup (RPO minutes); this is the portable, verifiable copy that
# the restore drill uses, and the way to move data between environments.
#
# What it leaves out: the *data* of django_session (after a restore everyone signs in
# again; a backup must not carry live sessions). Everything else, including the audit trail
# and the outbox, is kept. The dump holds every lead's personal data: it is created readable
# by its owner only; store it encrypted, with access restricted like the database itself,
# and delete it on the retention schedule (docs/privacy.md).
#
# Needs pg_dump and pg_restore from PostgreSQL 16 or newer (the server's major version).
set -euo pipefail
umask 077  # the dump and its checksum: owner-only

: "${DATABASE_URL:?set DATABASE_URL (the database to back up)}"
out="${1:-.}"
mkdir -p "$out"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
file="$out/arkray-$stamp.dump"

# The password goes to libpq through the environment, never on a command line (where any
# local user could read it in the process list for the length of the backup).
if [[ "$DATABASE_URL" =~ ^(postgres(ql)?://)([^:@/]+):([^@]*)@(.*)$ ]]; then
  password="${BASH_REMATCH[4]}"
  export PGPASSWORD="$(printf '%b' "${password//%/\\x}")"
  DATABASE_URL="${BASH_REMATCH[1]}${BASH_REMATCH[3]}@${BASH_REMATCH[5]}"
fi

started=$(date +%s)
pg_dump --format=custom --compress=6 --no-owner --no-privileges \
  --exclude-table-data=django_session \
  --file="$file" "$DATABASE_URL"
# The archive's table of contents must read back, or the backup is worthless.
pg_restore --list "$file" > /dev/null
(cd "$out" && sha256sum "$(basename "$file")" > "$(basename "$file").sha256")
echo "backup: $file ($(du -h "$file" | cut -f1), $(( $(date +%s) - started )) s)"
