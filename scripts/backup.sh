#!/usr/bin/env bash
# Logical backup of the Arkray database (docs/runbooks.md#restore-from-backup).
#
#   DATABASE_URL=postgres://user:pass@host:5432/arkray \
#   BACKUP_GPG_RECIPIENT_FILE=/etc/arkray/backup-public-key.asc \
#     scripts/backup.sh [output-dir] [--prune]
#
# Writes arkray-<UTC timestamp>.dump.gpg (pg_dump custom format, compressed, then encrypted
# to the backup public key) and a .sha256 of the encrypted file, after checking that the
# archive reads back. Managed PostgreSQL's point-in-time recovery is the primary backup (RPO
# minutes); this is the portable, verifiable copy the restore drill uses.
#
# Encryption (privacy remediation P2-8; docs/runbooks.md#backup-encryption-keys): the dump
# holds every lead's personal data, so it is only ever written encrypted. The host making
# backups holds the PUBLIC key only (BACKUP_GPG_RECIPIENT_FILE, an armoured OpenPGP key);
# the private key is kept offline by two named people and used only to restore. The
# plaintext exists only in a private temporary directory for the length of the check, and
# is removed even if the script fails. BACKUP_ALLOW_UNENCRYPTED=1 writes a plain .dump: for
# a throwaway local drill only, never for real data.
#
# What it leaves out: the *data* of django_session (after a restore everyone signs in
# again; a backup must not carry live sessions). Everything else, including the audit trail
# and the outbox, is kept. The erasure ledger is NOT in it (it lives outside the database):
# after a restore, `manage.py replay_erasures` re-applies every erasure made since.
#
# Retention: --prune deletes this directory's backups older than BACKUP_RETENTION_DAYS (30
# by default; the organisation's policy, not a legal requirement), always keeping the newest
# BACKUP_KEEP_MINIMUM (7), and never while a file named LEGAL_HOLD exists in the directory.
# It lists what it deletes.
#
# Needs pg_dump and pg_restore from PostgreSQL 16 or newer (the server's major version), and
# gpg.
set -euo pipefail
umask 077  # the dump and its checksum: owner-only

: "${DATABASE_URL:?set DATABASE_URL (the database to back up)}"
out="."
prune=0
for arg in "$@"; do
  case "$arg" in
    --prune) prune=1 ;;
    *) out="$arg" ;;
  esac
done
mkdir -p "$out"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"

encrypt=1
if [[ "${BACKUP_ALLOW_UNENCRYPTED:-}" == "1" ]]; then
  encrypt=0
  echo "backup: WARNING: writing an UNENCRYPTED dump (BACKUP_ALLOW_UNENCRYPTED=1): local drills only" >&2
else
  : "${BACKUP_GPG_RECIPIENT_FILE:?set BACKUP_GPG_RECIPIENT_FILE (the backup public key), or BACKUP_ALLOW_UNENCRYPTED=1 for a local drill}"
  [[ -r "$BACKUP_GPG_RECIPIENT_FILE" ]] || { echo "refusing: $BACKUP_GPG_RECIPIENT_FILE isn't readable" >&2; exit 1; }
fi

# The password goes to libpq through the environment, never on a command line (where any
# local user could read it in the process list for the length of the backup).
if [[ "$DATABASE_URL" =~ ^(postgres(ql)?://)([^:@/]+):([^@]*)@(.*)$ ]]; then
  password="${BASH_REMATCH[4]}"
  export PGPASSWORD="$(printf '%b' "${password//%/\\x}")"
  DATABASE_URL="${BASH_REMATCH[1]}${BASH_REMATCH[3]}@${BASH_REMATCH[5]}"
fi

work="$(mktemp -d)"
gnupg="$(mktemp -d)"
trap 'rm -rf "$work" "$gnupg"' EXIT
plain="$work/arkray-$stamp.dump"

started=$(date +%s)
pg_dump --format=custom --compress=6 --no-owner --no-privileges \
  --exclude-table-data=django_session \
  --file="$plain" "$DATABASE_URL"
# The archive's table of contents must read back, or the backup is worthless.
pg_restore --list "$plain" > /dev/null

if [[ "$encrypt" == "1" ]]; then
  file="$out/arkray-$stamp.dump.gpg"
  # A throwaway keyring: the recipient's public key only; nothing persists on this host.
  GNUPGHOME="$gnupg" gpg --batch --quiet --import "$BACKUP_GPG_RECIPIENT_FILE"
  GNUPGHOME="$gnupg" gpg --batch --quiet --yes --trust-model always \
    --recipient-file "$BACKUP_GPG_RECIPIENT_FILE" --output "$file" --encrypt "$plain"
else
  file="$out/arkray-$stamp.dump"
  mv "$plain" "$file"
fi
rm -f "$plain"
(cd "$out" && sha256sum "$(basename "$file")" > "$(basename "$file").sha256")
echo "backup: $file ($(du -h "$file" | cut -f1), $(( $(date +%s) - started )) s)"

if [[ "$prune" == "1" ]]; then
  if [[ -e "$out/LEGAL_HOLD" ]]; then
    echo "prune: skipped, $out/LEGAL_HOLD exists"
    exit 0
  fi
  days="${BACKUP_RETENTION_DAYS:-30}"
  keep="${BACKUP_KEEP_MINIMUM:-7}"
  mapfile -t all < <(ls -1t "$out"/arkray-*.dump.gpg "$out"/arkray-*.dump 2>/dev/null || true)
  index=0
  for candidate in "${all[@]}"; do
    index=$((index + 1))
    (( index <= keep )) && continue
    if [[ -n "$(find "$candidate" -maxdepth 0 -mtime +"$days" 2>/dev/null)" ]]; then
      echo "prune: deleting $candidate (older than $days days)"
      rm -f "$candidate" "$candidate.sha256"
    fi
  done
fi
