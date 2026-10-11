"""scripts/backup.sh and scripts/restore.sh: the transport guard and the restore's checks
(R128; adversarial review P1-1, P2-1, P3).

The guard (`require_verified_tls`, the same function in both scripts) is run here in bash
against URLs shaped to slip past a string match: each must be refused, or made to use
sslmode=verify-full, unless every address libpq would use is on this machine. The restore's
order of checks is read from the script; the real thing (a signed, encrypted backup and its
restore, a tampered copy and a planted plain dump both refused) is the restore drill
(tests/drills/restore_drill.py).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _bash() -> str | None:
    found = shutil.which("bash")
    if found and sys.platform == "win32" and "system32" in found.lower():
        found = None  # WSL's launcher, not a bash for this checkout
    if not found and sys.platform == "win32":
        candidate = Path(r"C:\Program Files\Git\bin\bash.exe")
        found = str(candidate) if candidate.exists() else None
    return found


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def _guard(script: str) -> str:
    text = (SCRIPTS / script).read_text(encoding="utf-8")
    start = text.index("refuse_url() {")
    end = text.index("\n}\n", text.index("require_verified_tls() {")) + 3
    return text[start:end]


def test_both_scripts_share_one_guard():
    assert _guard("backup.sh") == _guard("restore.sh")


def check(url: str, **env: str) -> tuple[bool, str]:
    """(accepted, the PGSSLMODE it leaves) for `url`. The program and the URL go through a
    file and the environment, never the command line (Git for Windows' bash rewrites
    backslashes and paths in its arguments)."""
    program = (
        _guard("backup.sh")
        + 'require_verified_tls "$GUARD_URL" DATABASE_URL'
        + chr(10)
        + 'echo "ok:${PGSSLMODE:-}"'
        + chr(10)
    )
    clean = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    assert BASH is not None
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "guard.sh"
        script.write_text(program, encoding="utf-8", newline=chr(10))
        result = subprocess.run(  # noqa: S603 — a fixed interpreter and our own script
            [BASH, script.name],
            cwd=tmp,
            capture_output=True,
            text=True,
            env={**clean, **env, "GUARD_URL": url},
            check=False,
            timeout=30,
        )
    out = result.stdout.strip()
    return (result.returncode == 0 and out.startswith("ok:"), out.removeprefix("ok:"))


@pytest.mark.parametrize(
    "url",
    [
        "postgres://u@127.0.0.1:5432/arkray",
        "postgres://u@127.1.2.3/arkray?sslmode=disable",
        "postgres://u@localhost/arkray",
        "postgresql://u@[::1]:5432/arkray",
        "postgres://u@/arkray?host=/var/run/postgresql",
        "postgres://u@%2Fvar%2Frun%2Fpostgresql/arkray",
        "postgres://u@/arkray",
    ],
)
def test_this_machine_needs_no_tls(url):
    accepted, mode = check(url)
    assert accepted
    assert mode == ""


@pytest.mark.parametrize(
    "url",
    [
        "postgres://u@db.example.com/arkray",
        "postgres://u@db.example.com/arkray?sslrootcert=/ca.crt",
        "postgres://u@127.attacker.example/arkray",
        "postgres://u@127.0.0.256/arkray",
        "postgres://u@localhost/arkray?hostaddr=203.0.113.9",
        "postgres://u@db.example.com/arkray?sslmode=verify-full",
    ],
)
def test_a_remote_server_gets_verify_full(url):
    accepted, mode = check(url)
    assert accepted
    assert mode in {"verify-full", ""}  # set here, or the URL's own verify-full


@pytest.mark.parametrize(
    "url",
    [
        # weaker modes for a remote server
        "postgres://u@db.example.com/arkray?sslmode=require",
        "postgres://u@db.example.com/arkray?sslmode=verify-ca",
        "postgres://u@127.attacker.example/arkray?sslmode=disable",
        "postgres://u@localhost/arkray?hostaddr=203.0.113.9&sslmode=disable",
        # shapes that slipped past the string match (adversarial review P3)
        "postgres://u@127.0.0.1:5432,db.example.com:5432/arkray?sslmode=disable",
        "postgres://u@127.0.0.1/arkray?host=127.0.0.1,db.example.com&sslmode=disable",
        "postgres://u@db.example.com/arkray?sslmode=verify-full&sslmode=disable",
        "postgres://u@127.0.0.1/arkray?host=127.0.0.1&host=db.example.com",
        "postgres://u@db.example.com/arkray?ssl%6dode=disable",
        "postgres://u@db.example.com/arkray?service=prod",
        "postgres://u@\\x31\\x32\\x37.0.0.1/arkray",
        # not a URL
        "host=db.example.com sslmode=disable",
        "mysql://u@db.example.com/arkray",
    ],
)
def test_bypasses_are_refused(url):
    accepted, _ = check(url)
    assert not accepted


def test_the_environment_cant_redirect_a_loopback_url():
    assert not check("postgres://u@127.0.0.1/arkray", PGSERVICE="prod")[0]
    accepted, mode = check("postgres://u@127.0.0.1/arkray", PGHOSTADDR="203.0.113.9")
    assert accepted
    assert mode == "verify-full"
    assert not check(
        "postgres://u@127.0.0.1/arkray", PGHOSTADDR="203.0.113.9", PGSSLMODE="require"
    )[0]
    accepted, mode = check("postgres://u@/arkray", PGHOST="db.example.com")
    assert accepted
    assert mode == "verify-full"


def test_restore_checks_private_copies_and_always_the_signature():
    """P2-1: after the copy, the checks read only the private copies (the store's path
    appears in messages alone) and the restore uses the copy; P1-1: the signature check
    doesn't depend on the file's name."""
    text = (SCRIPTS / "restore.sh").read_text(encoding="utf-8")
    copied = text.index('cp -- "$dump" "$copy"')
    switched = text.index('dump="$copy"')
    checks = text[copied:switched].splitlines()[1:]
    for line in checks:
        if "$dump" in line:
            assert line.lstrip().startswith(("cp -- ", 'if [[ -f "$dump.', "echo ")), line
    assert any('sha256sum < "$copy"' in line for line in checks)
    assert any('--verify "$copy.sig" "$copy"' in line for line in checks)
    assert text.index('--decrypt "$dump"') > switched
    assert text.index('--dbname="$TARGET_DATABASE_URL" "$dump"') > switched
    assert "*.gpg || -f" not in text
    signature = text[text.index("# The signature, for every dump") : switched]
    # One decision: verified, or RESTORE_UNVERIFIED=1.
    assert len(re.findall(r"^if \[\[ ", signature, re.M)) == 1
    assert 'if [[ -f "$copy.sig" && -n "${BACKUP_SIGNER_KEY_FILE:-}" ]]' in signature
    assert 'elif [[ "${RESTORE_UNVERIFIED:-}" == "1" ]]' in signature
