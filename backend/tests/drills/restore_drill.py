# ruff: noqa: T201, S108
# An operator's drill (prints its result), not application code: /tmp/drill is a directory
# inside the throwaway PostgreSQL container it drives.
"""A real backup/restore drill with synthetic erased subjects (privacy remediation P2-8;
docs/runbooks.md#restore-drill).

    python tests/drills/restore_drill.py --container arkray-postgres-1 --report drill.json

Against the local Compose PostgreSQL (its superuser), in two throwaway databases it creates
and drops:

1. seeds synthetic people: a customer with a deal, a note, a file and a custom value, a
   second customer, and a former staff member;
2. backs the source database up with scripts/backup.sh, encrypted to a throwaway OpenPGP key
   made for the drill (inside the PostgreSQL container: pg_dump and gpg);
3. after the backup: erases the customer, deletes the file and the custom field's values and
   pseudonymises the former staff member, each recorded in the erasure ledger (a temporary
   directory, as its own volume would be);
4. restores the backup into an empty database with scripts/restore.sh (decrypting it) and
   points the application at it: the ledger gate must refuse the API (everyone erased is
   back), and must also refuse it with the ledger unavailable;
5. runs `replay_erasures`: the customer is erased again, their index chunks and file gone,
   the custom values deleted, the staff member pseudonymised, and the API reopens;
6. writes the report (ids, outcomes, counts and timings; never personal data).

Each step is checked; the drill exits non-zero at the first failure.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
SOURCE_DB = "arkray_drill_source"
RESTORED_DB = "arkray_drill_restored"
DB_HOST = "127.0.0.1:55432"
SUPERUSER = "arkray"
PASSWORD = os.environ.get("DRILL_DB_PASSWORD", "arkray_dev_only_password")


def run(*command: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603 — fixed commands
        list(command), capture_output=True, text=True, env=env, check=False, timeout=900
    )
    if check and result.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(command[:4])}...\n{result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


def psql(container: str, sql: str, database: str = "postgres") -> str:
    return run("docker", "exec", container, "psql", "-U", SUPERUSER, "-d", database, "-tAc", sql)


def manage(env: dict[str, str], *args: str) -> str:
    return run(sys.executable, "manage.py", *args, env=env)


def django_env(database: str, work: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "DATABASE_"))}
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.local",
            "DJANGO_SECRET_KEY": "drill-only-secret-key-" + "0" * 40,
            "DATABASE_URL": f"postgres://{SUPERUSER}:{PASSWORD}@{DB_HOST}/{database}",
            "ERASURE_LEDGER_URL": (work / "ledger").as_uri(),
            "ERASURE_LEDGER_KEY": "drill-only-erasure-ledger-key-" + "1" * 32,
            "ERASURE_LEDGER_CHECK_INTERVAL_S": "0",
            "ATTACHMENT_ROOT": str(work / "attachments"),
            "EXPORT_ROOT": str(work / "exports"),
            "AI_ENABLED": "true",
            "AI_INDEXING_ENABLED": "true",
            "AI_EMBEDDING_PROVIDER": "hashing",
            "AI_LLM_PROVIDER": "none",
            "CELERY_BROKER_URL": "memory://",
            "PYTHONUTF8": "1",
        }
    )
    return env


STEP = r"""
import json, sys, django
django.setup()
from tests.drills import drill_steps
print("DRILL_RESULT " + json.dumps(getattr(drill_steps, sys.argv[1])(*sys.argv[2:])))
"""


def step(env: dict[str, str], name: str, *args: str) -> Any:
    output = run(sys.executable, "-c", STEP, name, *args, env=env)
    line = next(line for line in output.splitlines() if line.startswith("DRILL_RESULT "))
    return json.loads(line.removeprefix("DRILL_RESULT "))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", default="arkray-postgres-1")
    parser.add_argument("--report", required=True)
    options = parser.parse_args()
    container = options.container
    report: dict[str, Any] = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    work = Path(tempfile.mkdtemp(prefix="arkray-drill-"))
    for sub in ("ledger", "attachments", "exports"):
        (work / sub).mkdir()
    os.chdir(BACKEND)
    try:
        for database in (SOURCE_DB, RESTORED_DB):
            psql(container, f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
        psql(container, f"CREATE DATABASE {SOURCE_DB}")
        source = django_env(SOURCE_DB, work)
        manage(source, "migrate", "--noinput")
        report["seeded"] = seeded = step(source, "seed")

        # 2. The encrypted backup, inside the PostgreSQL container.
        run("docker", "exec", container, "rm", "-rf", "/tmp/drill")
        run("docker", "exec", container, "mkdir", "-p", "/tmp/drill/out", "/tmp/drill/gnupg")
        run("docker", "exec", container, "chmod", "700", "/tmp/drill/gnupg")
        for script in ("backup.sh", "restore.sh"):
            run("docker", "cp", str(ROOT / "scripts" / script), f"{container}:/tmp/drill/{script}")
        run(
            "docker",
            "exec",
            "-e",
            "GNUPGHOME=/tmp/drill/gnupg",
            container,
            "gpg",
            "--batch",
            "--passphrase",
            "",
            "--quick-generate-key",
            "arkray-drill@example.invalid",
            "ed25519",
            "cert",
            "never",
        )
        fingerprint = run(
            "docker",
            "exec",
            "-e",
            "GNUPGHOME=/tmp/drill/gnupg",
            container,
            "sh",
            "-c",
            "gpg --batch --with-colons --list-keys arkray-drill@example.invalid 2>/dev/null"
            " | awk -F: '/^fpr/ {print $10; exit}'",
        ).strip()
        run(
            "docker",
            "exec",
            "-e",
            "GNUPGHOME=/tmp/drill/gnupg",
            container,
            "gpg",
            "--batch",
            "--passphrase",
            "",
            "--quick-add-key",
            fingerprint,
            "cv25519",
            "encr",
            "never",
        )
        run(
            "docker",
            "exec",
            "-e",
            "GNUPGHOME=/tmp/drill/gnupg",
            container,
            "sh",
            "-c",
            "gpg --batch --armor --export arkray-drill@example.invalid > /tmp/drill/public.asc",
        )
        started = time.monotonic()
        backup_log = run(
            "docker",
            "exec",
            "-e",
            f"DATABASE_URL=postgres://{SUPERUSER}:{PASSWORD}@127.0.0.1:5432/{SOURCE_DB}",
            "-e",
            "BACKUP_GPG_RECIPIENT_FILE=/tmp/drill/public.asc",
            container,
            "bash",
            "/tmp/drill/backup.sh",
            "/tmp/drill/out",
        )
        dump = run("docker", "exec", container, "sh", "-c", "ls /tmp/drill/out/*.dump.gpg").strip()
        plain_check = run(
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            f"head -c 4096 {dump} | grep -c PGDMP || true",
        ).strip()
        report["backup"] = {
            "file": Path(dump).name,
            "seconds": round(time.monotonic() - started, 1),
            "encrypted": plain_check == "0",
            "log_tail": backup_log.strip().splitlines()[-1][:200],
        }
        if not report["backup"]["encrypted"]:
            raise SystemExit("FAILED: the backup is readable without the private key")

        # 3. Erasures after the backup, each in the ledger.
        report["erased"] = step(source, "erase_after_backup", json.dumps(seeded))
        report["ledger_entries"] = len(list((work / "ledger").glob("*.json")))

        # 4. Restore into an empty database, then the gate.
        psql(container, f"CREATE DATABASE {RESTORED_DB}")
        psql(container, "CREATE EXTENSION IF NOT EXISTS vector", RESTORED_DB)
        psql(container, "CREATE EXTENSION IF NOT EXISTS pg_trgm", RESTORED_DB)
        psql(container, "CREATE EXTENSION IF NOT EXISTS btree_gin", RESTORED_DB)
        psql(container, "CREATE EXTENSION IF NOT EXISTS pg_stat_statements", RESTORED_DB)
        started = time.monotonic()
        restore_log = run(
            "docker",
            "exec",
            "-e",
            f"TARGET_DATABASE_URL=postgres://{SUPERUSER}:{PASSWORD}@127.0.0.1:5432/{RESTORED_DB}",
            "-e",
            "BACKUP_GNUPGHOME=/tmp/drill/gnupg",
            container,
            "bash",
            "/tmp/drill/restore.sh",
            dump,
        )
        restored = django_env(RESTORED_DB, work)
        report["restore"] = {
            "seconds": round(time.monotonic() - started, 1),
            "checksum_ok": "checksum: ok" in restore_log,
            "decrypted": "decrypted: ok" in restore_log,
            "replay_reminder": "replay_erasures" in restore_log,
        }
        report["after_restore"] = step(restored, "inspect", json.dumps(seeded))
        if not report["after_restore"]["resurrected"]:
            raise SystemExit("FAILED: the drill expected the backup to bring the erased back")
        if report["after_restore"]["api_status"] != 503:
            raise SystemExit("FAILED: the API served a restored database before the replay")
        unavailable = dict(restored, ERASURE_LEDGER_URL=(work / "missing-ledger").as_uri())
        report["ledger_unavailable"] = step(unavailable, "gate_only")
        if report["ledger_unavailable"]["api_status"] != 503:
            raise SystemExit("FAILED: the API served with the ledger unavailable")

        # 5. Replay, then everything must hold again.
        replay_report = work / "replay-report.json"
        output = manage(
            restored,
            "replay_erasures",
            "--by",
            seeded["admin_email"],
            "--report",
            str(replay_report),
        )
        report["replay"] = json.loads(replay_report.read_text(encoding="utf-8"))
        report["replay_output"] = output.strip().splitlines()[-1][:300]
        report["after_replay"] = step(restored, "inspect", json.dumps(seeded))
        checks = report["after_replay"]
        failed = [
            name
            for name, ok in (
                ("lead erased", checks["lead_erased"]),
                ("no index chunks for the erased", checks["chunks"] == 0),
                ("file purged and forgotten", checks["attachment_forgotten"]),
                ("custom values deleted", checks["custom_values"] == 0),
                ("staff pseudonymised", checks["staff_pseudonymised"]),
                ("bystander untouched", checks["bystander_intact"]),
                ("api reopened", checks["api_status"] in (200, 204)),
            )
            if not ok
        ]
        report["result"] = "PASS" if not failed else f"FAIL: {', '.join(failed)}"
        again = manage(
            restored,
            "replay_erasures",
            "--by",
            seeded["admin_email"],
            "--report",
            str(work / "replay-again.json"),
        )
        report["replay_again"] = json.loads((work / "replay-again.json").read_text())["counts"]
        report["replay_again_output"] = again.strip().splitlines()[-1][:300]
    finally:
        for database in (SOURCE_DB, RESTORED_DB):
            psql(container, f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
        run("docker", "exec", container, "rm", "-rf", "/tmp/drill", check=False)
        shutil.rmtree(work, ignore_errors=True)
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        Path(options.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("result", "backup", "restore")}, indent=2))
    return 0 if report.get("result") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
