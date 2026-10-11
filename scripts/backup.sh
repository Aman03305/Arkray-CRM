#!/usr/bin/env bash
# Logical backup of the Arkray database (docs/runbooks.md#restore-from-backup).
#
#   DATABASE_URL='postgres://user:pass@host:5432/arkray?sslmode=verify-full&sslrootcert=/etc/arkray/db-ca.crt' \
#   BACKUP_GPG_RECIPIENT_FILE=/etc/arkray/backup-public-key.asc \
#   BACKUP_GPG_SIGNING_KEY_FILE=/etc/arkray/backup-signing-key.asc \
#     scripts/backup.sh [output-dir] [--prune]
#
# Writes arkray-<UTC timestamp>.dump.gpg (pg_dump custom format, compressed, then encrypted
# to the backup public key), a detached OpenPGP signature of the encrypted file (.sig) and
# a .sha256 of it, after checking that the archive reads back. The database connection must
# use verified TLS (sslmode=verify-full) unless the server is on this machine. Managed PostgreSQL's point-in-time recovery is the primary backup (RPO
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
# Signature (R128): encryption to a PUBLIC key proves nothing about who wrote a file (anyone
# holding that key can make one), and a .sha256 next to it is replaced as easily as the dump.
# So the encrypted file is signed with the backup host's own signing key
# (BACKUP_GPG_SIGNING_KEY_FILE, an armoured OpenPGP secret key that can sign, unprotected or
# with its passphrase in BACKUP_GPG_SIGNING_PASSPHRASE_FILE; readable by the backup job
# only), and restore.sh refuses a dump whose signature doesn't verify against the matching
# public key. Someone who can write to the backup store, but isn't the backup host, can
# neither plant nor alter a backup. Required whenever the dump is encrypted; a plain drill
# dump (BACKUP_ALLOW_UNENCRYPTED=1) is signed too when the key is given, and restore.sh
# refuses an unsigned one unless RESTORE_UNVERIFIED=1.
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
# gpg (2.1 or newer).
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
  : "${BACKUP_GPG_SIGNING_KEY_FILE:?set BACKUP_GPG_SIGNING_KEY_FILE (the backup host signing key)}"
fi
if [[ -n "${BACKUP_GPG_SIGNING_KEY_FILE:-}" ]]; then
  [[ -r "$BACKUP_GPG_SIGNING_KEY_FILE" ]] || { echo "refusing: $BACKUP_GPG_SIGNING_KEY_FILE isn't readable" >&2; exit 1; }
else
  echo "backup: WARNING: no BACKUP_GPG_SIGNING_KEY_FILE: the dump is unsigned (restore.sh needs RESTORE_UNVERIFIED=1)" >&2
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

# The password goes to libpq through the environment, never on a command line (where any
# local user could read it in the process list for the length of the backup).
if [[ "$DATABASE_URL" =~ ^(postgres(ql)?://)([^:@/]+):([^@]*)@(.*)$ ]]; then
  password="${BASH_REMATCH[4]}"
  export PGPASSWORD="$(printf '%b' "${password//%/\\x}")"
  DATABASE_URL="${BASH_REMATCH[1]}${BASH_REMATCH[3]}@${BASH_REMATCH[5]}"
fi
require_verified_tls "$DATABASE_URL" DATABASE_URL

work="$(mktemp -d)"
gnupg="$(mktemp -d)"
signing="$(mktemp -d)"
trap 'rm -rf "$work" "$gnupg" "$signing"' EXIT
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
if [[ -n "${BACKUP_GPG_SIGNING_KEY_FILE:-}" ]]; then
  # The signing key in a throwaway keyring of its own, removed on exit. The signature covers
  # the file as stored (the encrypted one when encrypted).
  GNUPGHOME="$signing" gpg --batch --quiet --import "$BACKUP_GPG_SIGNING_KEY_FILE"
  signer="$(GNUPGHOME="$signing" gpg --batch --with-colons --list-secret-keys 2>/dev/null \
    | awk -F: '/^fpr/ {print $10; exit}')"
  [[ -n "$signer" ]] || { echo "refusing: $BACKUP_GPG_SIGNING_KEY_FILE holds no secret key" >&2; exit 1; }
  passphrase=(--passphrase "")
  if [[ -n "${BACKUP_GPG_SIGNING_PASSPHRASE_FILE:-}" ]]; then
    passphrase=(--passphrase-file "$BACKUP_GPG_SIGNING_PASSPHRASE_FILE")
  fi
  GNUPGHOME="$signing" gpg --batch --quiet --yes --pinentry-mode loopback "${passphrase[@]}" \
    --local-user "$signer" --detach-sign --output "$file.sig" "$file"
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
      rm -f "$candidate" "$candidate.sha256" "$candidate.sig"
    fi
  done
fi
