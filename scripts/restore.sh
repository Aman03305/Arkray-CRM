#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh into an EMPTY database
# (docs/runbooks.md#restore-from-backup).
#
#   TARGET_DATABASE_URL=postgres://arkray_owner:pass@host:5432/arkray_restored \
#     scripts/restore.sh path/to/arkray-<stamp>.dump
#
# The target is prepared by infrastructure/postgres/roles.sql (the database, both roles, the
# extensions); run this as the schema owner (arkray_owner), then
# `manage.py grant_app_privileges arkray_app` for the application role. Steps:
#   1. verify the archive against the SHA-256 next to it (refused without one unless
#      RESTORE_UNVERIFIED=1);
#   2. refuse a target that already has tables (a restore never merges);
#   3. pg_restore (parallel, stop at the first error), without the extensions' own entries:
#      roles.sql created them as a superuser, and only their owner may comment on them
#      (Phase 11 review: the owner's restore failed there);
#   4. VACUUM (ANALYZE): a restored database has an empty visibility map and no statistics,
#      and the dashboard's index-only figures run about 3x slower until it is vacuumed
#      (Phase 10, R55);
#   5. print row counts to compare with the source.
# The Ask Arkray index is part of the dump; if it was left out or is suspect, rebuild it
# with `manage.py ai_reindex` (docs/operations.md#rebuild-the-rag-index).
set -euo pipefail
umask 077

dump="${1:?usage: TARGET_DATABASE_URL=... scripts/restore.sh <dump-file>}"
: "${TARGET_DATABASE_URL:?set TARGET_DATABASE_URL (an empty database)}"
jobs="${RESTORE_JOBS:-4}"
# Index builds during the restore (the vector index above all) spill to disk with the 64 MB
# default and take most of the time: give these sessions more (they're the only workload).
# One process per build: a parallel build keeps that memory in shared memory, which a
# container's /dev/shm may not have (Phase 11: 1 GB failed against a 256 MB /dev/shm).
export PGOPTIONS="${PGOPTIONS:--c maintenance_work_mem=${RESTORE_MAINTENANCE_WORK_MEM:-1GB} -c max_parallel_maintenance_workers=0}"

# The password goes to libpq through the environment, never on a command line.
if [[ "$TARGET_DATABASE_URL" =~ ^(postgres(ql)?://)([^:@/]+):([^@]*)@(.*)$ ]]; then
  password="${BASH_REMATCH[4]}"
  export PGPASSWORD="$(printf '%b' "${password//%/\\x}")"
  TARGET_DATABASE_URL="${BASH_REMATCH[1]}${BASH_REMATCH[3]}@${BASH_REMATCH[5]}"
fi

if [[ -f "$dump.sha256" ]]; then
  expected="$(cut -d' ' -f1 < "$dump.sha256")"
  actual="$(sha256sum < "$dump" | cut -d' ' -f1)"
  if [[ "$expected" != "$actual" ]]; then
    echo "refusing: $dump does not match its checksum" >&2
    exit 1
  fi
  echo "checksum: ok"
elif [[ "${RESTORE_UNVERIFIED:-}" == "1" ]]; then
  echo "checksum: none next to the dump; continuing unverified (RESTORE_UNVERIFIED=1)" >&2
else
  echo "refusing: no $dump.sha256 (set RESTORE_UNVERIFIED=1 to restore it anyway)" >&2
  exit 1
fi

tables=$(psql "$TARGET_DATABASE_URL" -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
if [[ "$tables" != "0" ]]; then
  echo "refusing: the target database already has $tables tables (restore into an empty one)" >&2
  exit 1
fi

# Every entry but the extensions and the comments on them (roles.sql made them).
list="$(mktemp)"
trap 'rm -f "$list"' EXIT
pg_restore --list "$dump" | grep -vE '^[0-9]+; [0-9]+ [0-9]+ (EXTENSION|COMMENT) - EXTENSION ' \
  | grep -vE '^[0-9]+; [0-9]+ [0-9]+ EXTENSION - ' > "$list"

started=$(date +%s)
pg_restore --no-owner --no-privileges --exit-on-error --jobs="$jobs" --use-list="$list" \
  --dbname="$TARGET_DATABASE_URL" "$dump"
restored=$(( $(date +%s) - started ))

started=$(date +%s)
psql "$TARGET_DATABASE_URL" -q -c "SET client_min_messages = error" -c "VACUUM (ANALYZE)"
vacuumed=$(( $(date +%s) - started ))

echo "restored in ${restored} s, vacuumed in ${vacuumed} s"
psql "$TARGET_DATABASE_URL" -tA -F ' ' -c "
  SELECT 'users', count(*) FROM identity_user
  UNION ALL SELECT 'leads', count(*) FROM leads_lead
  UNION ALL SELECT 'opportunities', count(*) FROM pipeline_opportunity
  UNION ALL SELECT 'activities', count(*) FROM activities_activity
  UNION ALL SELECT 'timeline_entries', count(*) FROM activities_timeline_entry
  UNION ALL SELECT 'audit_events', count(*) FROM audit_event
  UNION ALL SELECT 'knowledge_chunks', count(*) FROM ai_knowledge_chunk
  UNION ALL SELECT 'migrations', count(*) FROM django_migrations"
