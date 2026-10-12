# ruff: noqa: T201
# An operator's drill (prints its result), not application code.
"""Point-in-time recovery with the erasure ledger, end to end (docs/runbooks.md#restore-drill;
docs/deployment.md#backup-restore-and-disaster-recovery).

    python tests/drills/pitr_drill.py --report pitr-drill.json [--wal-load-mb 1024]

From the backend directory, with Docker and the development Redis (docker-compose.yml: the
application's cache) running; nothing else is used. It creates its own containers and
volumes (all named arkray_dr_pitr_*, from the production stack's pinned PostgreSQL image:
infrastructure/compose.production.yml) and removes them at the end, on failure too. What it
proves is the self-hosted equivalent of a managed service's point-in-time recovery (or of
WAL-G / pgBackRest restoring from object storage):

1. a throwaway PostgreSQL 16 + pgvector archiving its WAL (`wal_level=replica`,
   `archive_mode=on`, an `archive_command` copying each segment to an archive volume,
   `archive_timeout` 10 s); the database provisioned the production way:
   infrastructure/postgres/roles.sql with the roles renamed for the drill (arkray_dr_owner,
   arkray_dr_app), `migrate` as the owner, `grant_app_privileges`, then `check` and
   `migrate --check` as the application role;
2. a base backup (`pg_basebackup`, plain format, no WAL of its own: the archive holds it)
   while the database holds the schema only, so everything below comes back by WAL replay;
3. synthetic data written through the application as the application role
   (tests/drills/drill_steps.py: users, deals with their leads, notes, a file, Ask Arkray's
   index), one lead erased (the ledger's first entries, in a file ledger of the drill's
   own) and a browser signed in; the WAL switched and archived; every table's row count and
   checksum taken; the recovery target T read from the server's clock; then, after T, the
   browser signed out, the customer erased (ledgered) and a user, a deal, a note and a file
   added, and the WAL archived again;
4. the primary stopped (the loss); the base backup verified (`pg_verifybackup`) and copied
   into a new volume, and a NEW container started on it with a `restore_command` reading the
   archive, `recovery_target_time = T` and `recovery_target_action = promote`. Every table
   must be exactly as at T (count and checksum), the rows written after T absent, the lead
   erased before T still erased, and the customer erased after T back;
5. the application pointed at the recovered database with the SAME ledger: the API answers
   503 `erasure_reconciliation_required` (and 503 with the ledger unavailable) until
   `replay_erasures` (as the owner) has run; then the customer is erased again with their
   index chunks, the bystander untouched, the API open, and a second replay finds every entry
   applied; the browser that signed out after T is signed in again (a physical backup holds
   the sessions, a dump doesn't) until `django_session` is emptied, as the recovery runbook
   does; `grant_app_privileges`, `migrate --check`, the release check's database-role
   check (the application role passes, the owner is refused), the attachment reconciliation
   (the file uploaded after T is an orphan, nothing is missing) and `VACUUM (ANALYZE)`;
6. prints the timings (base backup, WAL replay and its rate, ledger replay, vacuum, the
   recovery's total) and PASS or FAIL with the failed checks, and writes the report: ids,
   counts, outcomes, timings and PostgreSQL's recovery log lines, never personal data.

`--wal-load-mb N` also writes about N MB of WAL before T (bulk rows of filler in a database of
their own, which the checksums don't read), so the replay is timed at a size that means
something; the archive and the two data volumes then hold about three times that on disk.

Each step is checked; the drill exits non-zero at the first step that fails, or at the end
if any check failed. Passwords are random per run and reach Docker through its environment
(`-e NAME`), never a command line.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:  # run as a script, like restore_drill.py
    sys.path.insert(0, str(BACKEND))

from tests.drills.restore_drill import django_env, manage, run, step  # noqa: E402

PREFIX = "arkray_dr_pitr"
PRIMARY, RECOVERED = f"{PREFIX}_primary", f"{PREFIX}_recovered"
DATA, ARCHIVE = f"{PREFIX}_data", f"{PREFIX}_archive"
BASE, RESTORED = f"{PREFIX}_base", f"{PREFIX}_restored"
DATABASE = PREFIX
ADMIN, OWNER, APP = "arkray_dr_admin", "arkray_dr_owner", "arkray_dr_app"
PGDATA = "/var/lib/postgresql/data"
# As the drill's superuser inside the container, over loopback (which the image trusts).
PSQL = ("psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1", "-U", ADMIN)
# A segment is never overwritten, and appears under its name only once copied whole.
ARCHIVE_COMMAND = (
    "test ! -f /archive/%f && cp %p /archive/%f.part && mv /archive/%f.part /archive/%f"
)
SERVER = [
    # No parallel maintenance: the container's /dev/shm is small (docs/deployment.md#database).
    "-c",
    "max_parallel_maintenance_workers=0",
    "-c",
    "shared_preload_libraries=pg_stat_statements",
]
RECOVERY_LOG = re.compile(
    r"starting point-in-time recovery|consistent recovery state|recovery stopping|redo done"
    r"|selected new timeline|archive recovery complete|ready to accept connections"
)


def docker_path() -> str:
    found = shutil.which("docker")
    if found is None:
        raise SystemExit("docker isn't on PATH")
    return found


DOCKER = docker_path()


def pinned_image() -> str:
    """The production stack's PostgreSQL image, by digest: the drill runs what production runs."""
    text = (ROOT / "infrastructure" / "compose.production.yml").read_text(encoding="utf-8")
    found = re.search(r"image: (pgvector/pgvector:\S+@sha256:[0-9a-f]{64})", text)
    if found is None:
        raise SystemExit("FAILED: no pinned pgvector image in compose.production.yml")
    return found.group(1)


def docker(*args: str, env: dict[str, str] | None = None) -> str:
    return run(DOCKER, *args, env=env)


def query(container: str, sql: str, database: str = "postgres") -> str:
    """The SQL on stdin (PSQL); rows unaligned, without headers."""
    result = subprocess.run(  # noqa: S603 — fixed commands
        [DOCKER, "exec", "-i", container, *PSQL, "-tA", "-d", database, "-f", "-"],
        input=sql,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    if result.returncode != 0:
        raise SystemExit(f"FAILED: psql on {container}\n{result.stderr.strip()[-2000:]}")
    return result.stdout.strip()


def answers(*command: str) -> bool:
    return (
        subprocess.run(  # noqa: S603 — fixed commands
            [DOCKER, *command], capture_output=True, check=False, timeout=60
        ).returncode
        == 0
    )


def wait_for(ready: Callable[[], bool], what: str, timeout: float) -> float:
    started = time.monotonic()
    while not ready():
        if time.monotonic() - started > timeout:
            raise SystemExit(f"FAILED: {what} within {timeout:.0f} s")
        time.sleep(0.5)
    return round(time.monotonic() - started, 1)


def accepting(container: str) -> bool:
    """Ready for connections; a server that stopped (a recovery that failed) ends the drill."""
    if run(DOCKER, "inspect", "-f", "{{.State.Running}}", container).strip() != "true":
        tail = run(DOCKER, "logs", "--tail", "30", container, check=False)
        raise SystemExit(f"FAILED: {container} stopped\n{tail}")
    return answers("exec", container, "pg_isready", "-q", "-h", "127.0.0.1", "-U", ADMIN)


def promoted(container: str) -> bool:
    if not accepting(container):
        return False
    try:
        return query(container, "SELECT pg_is_in_recovery()") == "f"
    except SystemExit:
        return False


def host_port(container: str) -> str:
    return docker("port", container, "5432/tcp").strip().splitlines()[0].rsplit(":", 1)[1]


def archive_wal(container: str) -> float:
    """Close the current WAL segment and wait until the archive holds it."""
    started = time.monotonic()
    segment = query(container, "SELECT pg_walfile_name(pg_switch_wal())")
    wait_for(
        lambda: answers("exec", container, "test", "-f", f"/archive/{segment}"),
        f"WAL segment {segment} archived",
        120,
    )
    return round(time.monotonic() - started, 1)


def fingerprint(container: str) -> dict[str, list[Any]]:
    """Every table of the database: its row count and an md5 of its rows in a fixed order."""
    tables = query(
        container,
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1",
        DATABASE,
    ).splitlines()
    parts = [
        f"SELECT '{name}', count(*),"  # noqa: S608 — names from the catalogue
        f" md5(coalesce(string_agg(r::text, E'\\n' ORDER BY r::text), ''))"
        f' FROM public."{name}" r'
        for name in tables
    ]
    rows = query(container, " UNION ALL ".join(parts) + ";", DATABASE)
    found: dict[str, list[Any]] = {}
    for line in rows.splitlines():
        name, count, digest = line.split("|")
        found[name] = [int(count), digest]
    return found


def remove_everything() -> None:
    for command in (
        ["rm", "-f", "-v", PRIMARY, RECOVERED],
        ["volume", "rm", "-f", DATA, ARCHIVE, BASE, RESTORED],
    ):
        subprocess.run(  # noqa: S603 — fixed commands
            [DOCKER, *command], capture_output=True, check=False, timeout=300
        )


def leftovers() -> list[str]:
    found = []
    for command in (
        ["ps", "-a", "--filter", f"name={PREFIX}", "--format", "{{.Names}}"],
        ["volume", "ls", "--filter", f"name={PREFIX}", "--format", "{{.Name}}"],
    ):
        found += run(DOCKER, *command, check=False).split()
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--image", help="default: compose.production.yml's pinned pgvector")
    parser.add_argument(
        "--wal-load-mb",
        type=int,
        default=0,
        help="also write about this much WAL before T (bulk rows in a database of their own),"
        " to time the replay at a size that means something",
    )
    options = parser.parse_args()
    image = options.image or pinned_image()
    report: dict[str, Any] = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "image": image,
    }
    timings: dict[str, float] = {}
    checks: dict[str, bool] = {}
    report["timings"], report["checks"] = timings, checks
    passwords = {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "OWNER_PASSWORD": secrets.token_urlsafe(24),
        "APP_PASSWORD": secrets.token_urlsafe(24),
    }
    secret_env = {**os.environ, **passwords}
    work = Path(tempfile.mkdtemp(prefix="arkray-dr-pitr-"))
    for sub in ("ledger", "attachments", "exports"):
        (work / sub).mkdir()
    os.chdir(BACKEND)

    def timed(name: str, started: float) -> None:
        timings[name] = round(time.monotonic() - started, 1)

    def app_envs(container: str) -> tuple[dict[str, str], dict[str, str]]:
        """The owner's environment (migrate, grant, replay) and the application's."""
        port = host_port(container)
        envs = []
        for role, password in ((OWNER, "OWNER_PASSWORD"), (APP, "APP_PASSWORD")):
            env = django_env(DATABASE, work)
            env["DATABASE_URL"] = (
                f"postgres://{role}:{passwords[password]}@127.0.0.1:{port}/{DATABASE}"
            )
            envs.append(env)
        return envs[0], envs[1]

    try:
        remove_everything()  # an interrupted run's
        # 1. The primary, archiving its WAL, and the database provisioned as in production.
        for volume in (DATA, ARCHIVE, BASE, RESTORED):
            docker("volume", "create", volume)
        docker(
            "run",
            "--rm",
            "-v",
            f"{ARCHIVE}:/archive",
            "-v",
            f"{BASE}:/basebackup",
            "--entrypoint",
            "chown",
            image,
            "postgres:postgres",
            "/archive",
            "/basebackup",
        )
        started = time.monotonic()
        docker(
            "run",
            "-d",
            "--name",
            PRIMARY,
            "--shm-size=256m",
            "--memory=512m",
            "-e",
            f"POSTGRES_USER={ADMIN}",
            "-e",
            "POSTGRES_PASSWORD",
            "-v",
            f"{DATA}:{PGDATA}",
            "-v",
            f"{ARCHIVE}:/archive",
            "-v",
            f"{BASE}:/basebackup",
            "-p",
            "127.0.0.1::5432",
            image,
            "postgres",
            "-c",
            "wal_level=replica",
            "-c",
            "archive_mode=on",
            "-c",
            f"archive_command={ARCHIVE_COMMAND}",
            "-c",
            "archive_timeout=10s",
            *SERVER,
            env=secret_env,
        )
        wait_for(lambda: accepting(PRIMARY), "the primary accepting connections", 180)
        timed("primary_start_s", started)
        roles = (ROOT / "infrastructure" / "postgres" / "roles.sql").read_text(encoding="utf-8")
        roles = roles.replace("arkray_owner", OWNER).replace("arkray_app", APP)
        provision = subprocess.run(  # noqa: S603 — fixed commands
            [
                DOCKER,
                "exec",
                "-i",
                "-e",
                "OWNER_PASSWORD",
                "-e",
                "APP_PASSWORD",
                PRIMARY,
                *PSQL,
                "-d",
                "postgres",
                "-v",
                f"database={DATABASE}",
                "-f",
                "-",
            ],
            input=roles,
            capture_output=True,
            text=True,
            env=secret_env,
            check=False,
            timeout=300,
        )
        checks["roles.sql ran"] = provision.returncode == 0
        if provision.returncode != 0:
            raise SystemExit(f"FAILED: roles.sql\n{provision.stderr[-2000:]}")
        owner, app = app_envs(PRIMARY)
        started = time.monotonic()
        manage(owner, "migrate", "--noinput")
        timed("migrate_s", started)
        report["grant"] = manage(owner, "grant_app_privileges", APP).strip().splitlines()[-1]
        manage(app, "check", "--database", "default")
        manage(app, "migrate", "--check")
        checks["provisioned: migrate, grant, check, migrate --check"] = True

        # 2. The base backup: the schema only, so the data below can come back by replay alone.
        started = time.monotonic()
        backup_log = run(
            DOCKER,
            "exec",
            "-u",
            "postgres",
            PRIMARY,
            "pg_basebackup",
            "-h",
            "127.0.0.1",
            "-U",
            ADMIN,
            "-D",
            "/basebackup/base",
            "--format=plain",
            "--wal-method=none",
            "--checkpoint=fast",
            "--label=arkray-dr-pitr",
        )
        timed("base_backup_s", started)
        report["base_backup_log"] = backup_log.strip().splitlines()[-1:]
        backup_end = query(PRIMARY, "SELECT pg_current_wal_lsn()")

        # 3. Writes before T, T, writes after T.
        started = time.monotonic()
        seeded = step(app, "seed")
        ids = {**seeded, **step(app, "pitr_before_target", json.dumps(seeded))}
        timed("writes_before_target_s", started)
        if options.wal_load_mb:
            # Bulk rows (about 1 KB each) in a database of their own: WAL is the whole
            # cluster's, so the replay below is timed at a size that means something.
            started = time.monotonic()
            query(PRIMARY, f"CREATE DATABASE {PREFIX}_load")
            query(
                PRIMARY,
                "CREATE TABLE load (id bigint, filler text); INSERT INTO load"  # noqa: S608 — an int
                " SELECT n, repeat(md5(n::text), 30)"
                f" FROM generate_series(1, {options.wal_load_mb * 1000}) n;",
                f"{PREFIX}_load",
            )
            timed("wal_load_s", started)
        timings["archive_wait_before_target_s"] = archive_wal(PRIMARY)
        at_target = fingerprint(PRIMARY)
        target = query(
            PRIMARY,
            "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC',"
            " 'YYYY-MM-DD HH24:MI:SS.US') || '+00'",
        )
        report["target_time"] = target
        wal_bytes = query(
            PRIMARY, f"SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '{backup_end}')::bigint"
        )
        report["wal_to_replay_mb"] = round(int(wal_bytes) / 2**20, 1)
        report["ledger_entries_at_target"] = at_t = len(list((work / "ledger").glob("*.json")))
        time.sleep(1)  # every commit below is later than T
        started = time.monotonic()
        ids |= step(app, "pitr_after_target", json.dumps(ids))
        session_key = ids.pop("session_key")  # a credential, if only for this throwaway database
        timed("writes_after_target_s", started)
        timings["archive_wait_after_target_s"] = archive_wal(PRIMARY)
        report["ledger_entries"] = entries = len(list((work / "ledger").glob("*.json")))
        changed = [name for name, row in fingerprint(PRIMARY).items() if at_target[name] != row]
        report["tables_changed_after_target"] = changed
        checks["the writes after T changed the source"] = (
            bool(changed)
            and entries > at_t
            and ids["session_before_sign_out"] == 200
            and ids["session_after_sign_out"] != 200
        )
        report["ids"] = ids
        report["rows_at_target"] = {name: row[0] for name, row in at_target.items() if row[0]}

        # 4. The primary is lost; recover the base backup and the archive to T in a new server.
        docker("stop", "-t", "30", PRIMARY)
        lost = time.monotonic()
        started = time.monotonic()
        restore_log = docker(
            "run",
            "--rm",
            "-v",
            f"{BASE}:/basebackup:ro",
            "-v",
            f"{ARCHIVE}:/archive:ro",
            "-v",
            f"{RESTORED}:/restore",
            "--entrypoint",
            "sh",
            image,
            "-c",
            "gosu postgres pg_verifybackup --no-parse-wal /basebackup/base"
            " && cp -a /basebackup/base/. /restore/ && touch /restore/recovery.signal"
            " && chown -R postgres:postgres /restore && chmod 700 /restore"
            " && echo base: $(du -sk /basebackup/base | cut -f1) KB"
            " && echo archive: $(ls /archive | wc -l) segments $(du -sk /archive | cut -f1) KB",
        )
        timed("restore_copy_s", started)
        report["restore_log"] = restore_log.strip().splitlines()
        checks["base backup verified (pg_verifybackup)"] = "successfully verified" in restore_log
        started = time.monotonic()
        docker(
            "run",
            "-d",
            "--name",
            RECOVERED,
            "--shm-size=256m",
            "--memory=512m",
            "-v",
            f"{RESTORED}:{PGDATA}",
            "-v",
            f"{ARCHIVE}:/archive:ro",
            "-p",
            "127.0.0.1::5432",
            image,
            "postgres",
            "-c",
            "restore_command=cp /archive/%f %p",
            "-c",
            f"recovery_target_time={target}",
            "-c",
            "recovery_target_action=promote",
            # A recovered server archives to a NEW place in production; here, nowhere.
            "-c",
            "archive_mode=off",
            *SERVER,
        )
        wait_for(lambda: promoted(RECOVERED), "recovery to T and promotion", 1800)
        # From the container's start to promotion: the server's start, every segment
        # copied from the archive and replayed, the end-of-recovery checkpoint.
        timed("wal_replay_s", started)
        timings["wal_replay_mb_per_s"] = round(
            report["wal_to_replay_mb"] / max(timings["wal_replay_s"], 0.1), 1
        )
        log = run(DOCKER, "logs", RECOVERED)
        report["recovery_log"] = [line for line in log.splitlines() if RECOVERY_LOG.search(line)]
        report["wal_segments_restored"] = len(re.findall(r'restored log file "', log))
        checks["recovery stopped at T and promoted"] = (
            "recovery stopping before commit" in log and "archive recovery complete" in log
        )
        recovered = fingerprint(RECOVERED)
        report["tables_differing_from_target"] = differing = sorted(
            name
            for name in at_target.keys() | recovered.keys()
            if at_target.get(name) != recovered.get(name)
        )
        checks["every table exactly as at T"] = not differing

        # 5. The application on the recovered database, with the same ledger.
        owner, app = app_envs(RECOVERED)
        report["before_replay"] = before = step(app, "pitr_inspect", json.dumps(ids))
        checks["rows written after T absent"] = not any(before["late_rows"].values())
        checks["lead erased before T still erased"] = before["early_erased"]
        checks["customer erased after T is back"] = before["customer_back"]
        checks["API closed (503 erasure_reconciliation_required)"] = (
            before["gate"] == "behind"
            and before["api_status"] == 503
            and before["api_code"] == "erasure_reconciliation_required"
        )
        unavailable = dict(app, ERASURE_LEDGER_URL=(work / "missing-ledger").as_uri())
        report["ledger_unavailable"] = step(unavailable, "gate_only")
        checks["API closed with the ledger unavailable"] = (
            report["ledger_unavailable"]["api_status"] == 503
        )
        started = time.monotonic()
        replay_output = manage(
            owner,
            "replay_erasures",
            "--by",
            ids["admin_email"],
            "--report",
            str(work / "replay.json"),
        )
        timed("ledger_replay_s", started)
        replay = json.loads((work / "replay.json").read_text(encoding="utf-8"))
        report["replay"] = {
            k: replay[k] for k in ("complete", "applied_before", "applied_after", "counts")
        }
        report["replay_output"] = replay_output.strip().splitlines()[-1][:300]
        checks["replay re-applied only what came after T"] = (
            replay["complete"]
            and replay["applied_before"] == at_t
            and replay["applied_after"] == entries
            and replay["counts"] == {"already_applied": at_t, "reapplied": entries - at_t}
        )
        report["after_replay"] = after = step(app, "pitr_inspect", json.dumps(ids))
        timed("recovery_to_api_open_s", lost)
        checks["customer erased again, index chunks gone"] = (
            after["customer_erased"] and after["customer_chunks"] == 0
        )
        checks["bystander untouched, early erasure kept"] = (
            after["bystander_intact"] and after["early_erased"]
        )
        checks["API reopened"] = after["gate"] == "ok" and after["api_status"] in (200, 204)
        manage(
            owner,
            "replay_erasures",
            "--by",
            ids["admin_email"],
            "--report",
            str(work / "again.json"),
        )
        again = json.loads((work / "again.json").read_text(encoding="utf-8"))["counts"]
        report["replay_again"] = again
        checks["a second replay finds everything applied"] = again == {"already_applied": entries}
        # Sessions are in a physical backup (a dump leaves them out): the browser that signed
        # out after T is signed in again, until the sessions table is emptied.
        revived = step(app, "pitr_session", session_key)["status"]
        query(RECOVERED, "DELETE FROM django_session;", DATABASE)
        ended = step(app, "pitr_session", session_key)["status"]
        report["session_signed_out_after_target"] = {"recovered": revived, "emptied": ended}
        checks["a session ended after T is back until django_session is emptied"] = (
            revived == 200 and ended in (401, 403)
        )

        # Validation, as after any recovery (docs/runbooks.md#restore-from-backup).
        started = time.monotonic()
        report["grant_after"] = manage(owner, "grant_app_privileges", APP).strip().splitlines()[-1]
        timed("grant_s", started)
        manage(app, "migrate", "--check")
        manage(app, "check", "--database", "default")
        checks["migrate --check and check clean"] = True
        release = {"DB_REQUIRE_RESTRICTED_ROLE": "true"}
        as_app = run(
            sys.executable,
            "manage.py",
            "check",
            "--deploy",
            "--database",
            "default",
            env={**app, **release},
            check=False,
        )
        as_owner = run(
            sys.executable,
            "manage.py",
            "check",
            "--deploy",
            "--database",
            "default",
            env={**owner, **release},
            check=False,
        )
        checks["release check: application role passes, owner refused"] = (
            "arkray.E001" not in as_app
            and "arkray.W002" not in as_app
            and "arkray.E001" in as_owner
        )
        reconciled = run(
            sys.executable, "manage.py", "reconcile_attachments", "--json", env=app, check=False
        )
        if "{" not in reconciled:
            raise SystemExit(f"FAILED: reconcile_attachments\n{reconciled[-2000:]}")
        attachments = json.JSONDecoder().raw_decode(reconciled[reconciled.index("{") :])[0]
        report["attachments"] = {
            k: attachments.get(k) for k in ("exit_code", "healthy", "missing", "orphaned")
        }
        checks["attachments: the file after T is the one orphan, none missing"] = (
            attachments.get("orphaned") == 1 and attachments.get("missing") == 0
        )
        started = time.monotonic()
        query(RECOVERED, "VACUUM (ANALYZE);", DATABASE)
        timed("vacuum_analyze_s", started)
        timed("recovery_with_validation_s", lost)
        timings["database_rto_s"] = round(
            timings["restore_copy_s"]
            + timings["wal_replay_s"]
            + timings["ledger_replay_s"]
            + timings["vacuum_analyze_s"],
            1,
        )
        failed = [name for name, ok in checks.items() if not ok]
        report["result"] = "PASS" if not failed else f"FAIL: {', '.join(failed)}"
    finally:
        remove_everything()
        shutil.rmtree(work, ignore_errors=True)
        report["left_behind"] = leftovers()
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        Path(options.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    if report["left_behind"]:
        report["result"] = f"FAIL: left behind {report['left_behind']}"
    summary = (
        "result",
        "target_time",
        "wal_to_replay_mb",
        "timings",
        "recovery_log",
        "replay",
        "attachments",
    )
    print(json.dumps({k: report.get(k) for k in summary}, indent=2))
    return 0 if report.get("result") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
