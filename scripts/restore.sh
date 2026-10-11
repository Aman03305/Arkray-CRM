#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh into an EMPTY database
# (docs/runbooks.md#restore-from-backup).
#
#   TARGET_DATABASE_URL='postgres://arkray_owner:pass@host:5432/arkray_restored?sslmode=verify-full&sslrootcert=/secure/db-ca.crt' \
#   BACKUP_GNUPGHOME=/secure/restore-keyring \
#   BACKUP_SIGNER_KEY_FILE=/secure/backup-signing-public-key.asc \
#     scripts/restore.sh path/to/arkray-<stamp>.dump.gpg
#
# The target is prepared by infrastructure/postgres/roles.sql (the database, both roles, the
# extensions); run this as the schema owner (arkray_owner), then
# `manage.py grant_app_privileges arkray_app` for the application role. Steps:
#   1. copy the dump, its .sha256 and its .sig into a private working directory
#      (RESTORE_WORK_DIR, else TMPDIR, else /tmp: room for the dump twice when encrypted),
#      and from then on read only those copies: the backup store is never read again
#      between a check and the restore (adversarial review P2-1). Verify the copy against
#      the SHA-256 and, ALWAYS, whatever the file's name, against its signature (.sig) with
#      the backup host's PUBLIC signing key (BACKUP_SIGNER_KEY_FILE): a checksum alone is
#      replaced as easily as the dump, the signature is not (R128; adversarial review P1-1:
#      a planted plain .dump with no .sig was restored). Anything missing is refused unless
#      RESTORE_UNVERIFIED=1;
#   2. refuse a target that already has tables (a restore never merges);
#   3. pg_restore (parallel, stop at the first error), without the extensions' own entries:
#      roles.sql created them as a superuser, and only their owner may comment on them
#      (Phase 11 review: the owner's restore failed there);
#   4. VACUUM (ANALYZE): a restored database has an empty visibility map and no statistics,
#      and the dashboard's index-only figures run about 3x slower until it is vacuumed
#      (Phase 10, R55);
#   5. print row counts to compare with the source.
# An encrypted backup (.dump.gpg) is decrypted first, in the private working directory
# removed afterwards, with the keyring in BACKUP_GNUPGHOME (the backup private key: kept
# offline, brought in for the restore only; docs/runbooks.md#backup-encryption-keys). The
# checksum and the signature are checked on the encrypted copy.
#
# BEFORE ANY TRAFFIC: the restored database is behind the erasure ledger (everyone erased
# since the backup is back). The application keeps its API closed until, with the owner's
# credentials, `manage.py replay_erasures --by <admin> --report <file>` has re-applied every
# erasure (docs/runbooks.md#restore-from-backup); keep its report as the restore's evidence.
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

# Transport (R128): the dump crosses the network in full, so it must go over TLS with the
# server's certificate and name checked: sslmode=verify-full (set here when neither the URL
# nor PGSSLMODE names a mode), with the server's CA in `sslrootcert=<file>` (URL) or
# PGSSLROOTCERT (`sslrootcert=system` for a public CA). The one exemption is a server on
# this machine, decided the way libpq reads the URL (adversarial review P3: a string match
# let `127.attacker.example`, a second host, `hostaddr=`, a repeated `sslmode` or a
# percent-encoded name through). Refused outright: anything but a postgres:// URL, several
# hosts, a repeated `host` or `sslmode`, percent-encoded parameter names, backslashes, a
# service file (`service=`, PGSERVICE). Exempt only when every address libpq would use is
# loopback (127.0.0.0/8 as an address, ::1, localhost) or a Unix socket directory.
refuse_url() {
  echo "refusing: $1" >&2
  exit 1
}
pct_decode() {
  printf '%b' "${1//%/\\x}"
}
is_local_host() {
  local host="$1" octet
  local ipv4_loopback='^127\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$'
  case "$host" in
    localhost | ::1 | 0:0:0:0:0:0:0:1) return 0 ;;
    /*) return 0 ;;  # a Unix socket directory
  esac
  if [[ "$host" =~ $ipv4_loopback ]]; then
    for octet in "${BASH_REMATCH[@]:1}"; do
      (( 10#$octet <= 255 )) || return 1
    done
    return 0
  fi
  return 1
}
require_verified_tls() {
  local url="$1" name="$2" authority hostpart query pair key value
  local host="" hostaddr="${PGHOSTADDR:-}" mode="" modes=0 hosts=0
  local shape='^postgres(ql)?://([^/?#]*)(/[^?#]*)?(\?([^#]*))?$'
  local bracketed='^\[([^]]*)\](:[0-9]*)?$' plain='^([^:]*)(:[0-9]*)?$'
  [[ "$url" =~ $shape ]] || refuse_url "$name must be a postgres:// URL"
  [[ "$url" != *\\* ]] || refuse_url "$name has a backslash"
  [[ -z "${PGSERVICE:-}" ]] || refuse_url "PGSERVICE is set: name the server in $name instead"
  authority="${BASH_REMATCH[2]}"
  query="${BASH_REMATCH[5]}"
  hostpart="${authority##*@}"
  [[ "$hostpart" != *,* ]] || refuse_url "$name names several hosts: give one"
  if [[ "$hostpart" =~ $bracketed || "$hostpart" =~ $plain ]]; then
    host="$(pct_decode "${BASH_REMATCH[1]}")"
  else
    refuse_url "$name: unreadable host and port"
  fi
  local -a pairs=()
  [[ -z "$query" ]] || IFS='&' read -r -a pairs <<< "$query"
  for pair in "${pairs[@]}"; do
    [[ -n "$pair" ]] || continue
    key="${pair%%=*}"
    value="$(pct_decode "${pair#*=}")"
    [[ "$key" != *%* ]] || refuse_url "$name: percent-encoded parameter names aren't accepted"
    case "$key" in
      sslmode) mode="$value"; modes=$((modes + 1)) ;;
      host) host="$value"; hosts=$((hosts + 1)) ;;
      hostaddr) hostaddr="$value" ;;
      service) refuse_url "$name: a service file isn't accepted, name the server" ;;
    esac
  done
  (( modes <= 1 )) || refuse_url "$name gives sslmode more than once"
  (( hosts <= 1 )) || refuse_url "$name gives host more than once"
  [[ "$host" != *,* && "$hostaddr" != *,* ]] || refuse_url "$name names several hosts: give one"
  # No host at all: PGHOST, else libpq's default Unix socket.
  [[ -n "$host" ]] || host="${PGHOST:-/var/run/postgresql}"
  if is_local_host "$host" && { [[ -z "$hostaddr" ]] || is_local_host "$hostaddr"; }; then
    return 0  # this machine: nothing crosses a network
  fi
  if (( modes == 0 )); then
    mode="${PGSSLMODE:-verify-full}"
    export PGSSLMODE="$mode"
  fi
  if [[ "$mode" != "verify-full" ]]; then
    refuse_url "$name connects to ${hostaddr:-$host} with sslmode=$mode; use sslmode=verify-full (and the server's CA in sslrootcert)"
  fi
}
require_verified_tls "$TARGET_DATABASE_URL" TARGET_DATABASE_URL

[[ -f "$dump" ]] || { echo "refusing: $dump isn't a file" >&2; exit 1; }
work="$(mktemp -d "${RESTORE_WORK_DIR:-${TMPDIR:-/tmp}}/arkray-restore.XXXXXX")"
cleanup() { rm -rf "$work"; }
trap cleanup EXIT

# Private copies first: every check and the restore itself read these, never the store.
name="$(basename "$dump")"
copy="$work/$name"
cp -- "$dump" "$copy"
if [[ -f "$dump.sha256" ]]; then cp -- "$dump.sha256" "$copy.sha256"; fi
if [[ -f "$dump.sig" ]]; then cp -- "$dump.sig" "$copy.sig"; fi

if [[ -f "$copy.sha256" ]]; then
  expected="$(cut -d' ' -f1 < "$copy.sha256")"
  actual="$(sha256sum < "$copy" | cut -d' ' -f1)"
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

# The signature, for every dump whatever its name: made by backup.sh with the backup host's
# signing key, checked against its public key alone (a keyring holding nothing else, so a
# valid signature can only be that key's).
if [[ -f "$copy.sig" && -n "${BACKUP_SIGNER_KEY_FILE:-}" ]]; then
  mkdir -m 700 "$work/verify"
  GNUPGHOME="$work/verify" gpg --batch --quiet --import "$BACKUP_SIGNER_KEY_FILE"
  status="$(GNUPGHOME="$work/verify" gpg --batch --status-fd 1 --verify "$copy.sig" "$copy" 2>/dev/null || true)"
  if ! grep -q '^\[GNUPG:\] VALIDSIG ' <<< "$status"; then
    echo "refusing: $dump is not signed by the backup signing key" >&2
    exit 1
  fi
  echo "signature: ok"
elif [[ "${RESTORE_UNVERIFIED:-}" == "1" ]]; then
  echo "signature: not checked (no $dump.sig or no BACKUP_SIGNER_KEY_FILE); continuing unverified (RESTORE_UNVERIFIED=1)" >&2
else
  echo "refusing: $dump has no verifiable signature (needs $dump.sig and BACKUP_SIGNER_KEY_FILE; RESTORE_UNVERIFIED=1 to restore it anyway)" >&2
  exit 1
fi

dump="$copy"
if [[ "$dump" == *.gpg ]]; then
  : "${BACKUP_GNUPGHOME:?set BACKUP_GNUPGHOME (the keyring holding the backup private key)}"
  decrypted="${dump%.gpg}"
  GNUPGHOME="$BACKUP_GNUPGHOME" gpg --batch --quiet --output "$decrypted" --decrypt "$dump"
  rm -f "$dump"  # the encrypted copy: room for the restore
  echo "decrypted: ok"
  dump="$decrypted"
fi

tables=$(psql "$TARGET_DATABASE_URL" -tAc \
  "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
if [[ "$tables" != "0" ]]; then
  echo "refusing: the target database already has $tables tables (restore into an empty one)" >&2
  exit 1
fi

# Every entry but the extensions and the comments on them (roles.sql made them).
list="$work/restore.list"
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
echo "NEXT, before any traffic: manage.py replay_erasures --by <admin email> --report <file>"
echo "(the API stays closed until it has re-applied every erasure made since this backup)"
