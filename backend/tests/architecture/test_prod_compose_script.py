"""scripts/prod-compose.sh: Compose for the production-shaped stack, its secrets file the only
source of its secrets (review round 1 of the adversarial review of 73ff1e3, INFRA-5).

Compose gives a variable exported in the shell precedence over `--env-file`, so a stale
`ARKRAY_PROD_*` left in an operator's shell (the `set -a; . file` the docs once offered,
another environment's file sourced earlier) silently replaced the file's value. The wrapper
unsets every `ARKRAY_PROD_*` (and `COMPOSE_ENV_FILES`) before it runs `docker compose` with
the file, the project and both Compose files; it refuses a missing file and one inside the
checkout. Run here in bash against a stand-in `docker` that records what it was given.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.architecture.test_backup_scripts import BASH

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "prod-compose.sh"
pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")

FAKE_DOCKER = (
    "#!/usr/bin/env bash\n"
    'printf "%s\\n" "$@" > "$FAKE_OUT/args"\n'
    'env | grep -E "^(ARKRAY_PROD_|COMPOSE_ENV_FILES)" > "$FAKE_OUT/env" || true\n'
    'pwd > "$FAKE_OUT/cwd"\n'
)


def _run(
    tmp_path: Path, *args: str, **environ: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    assert BASH is not None
    bin_dir, out = tmp_path / "bin", tmp_path / "out"
    bin_dir.mkdir(exist_ok=True)
    out.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_bytes(FAKE_DOCKER.encode())
    docker.chmod(0o755)
    path = bin_dir.as_posix() if os.name != "nt" else "/" + bin_dir.as_posix().replace(":", "", 1)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("ARKRAY_PROD_")},
        "FAKE_OUT": out.as_posix(),
        **environ,
    }
    result = subprocess.run(  # noqa: S603 — fixed program and arguments
        [BASH, "-c", f'export PATH="{path}:$PATH"; exec "$0" "$@"', SCRIPT.as_posix(), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return result, out


def _secrets(tmp_path: Path) -> Path:
    secrets_file = tmp_path / "arkray-prod.env"
    secrets_file.write_text("ARKRAY_PROD_DB_APP_PASSWORD=from-the-file\n", encoding="utf-8")
    return secrets_file


def test_the_shells_production_secrets_never_reach_compose(tmp_path):
    secrets_file = _secrets(tmp_path)
    result, out = _run(
        tmp_path,
        secrets_file.as_posix(),
        "up",
        "-d",
        ARKRAY_PROD_DB_APP_PASSWORD="stale-value-in-the-shell",
        ARKRAY_PROD_DJANGO_SECRET_KEY="another-stale-value",
        COMPOSE_ENV_FILES="/elsewhere/.env",
    )
    assert result.returncode == 0, result.stderr
    assert (out / "env").read_text(encoding="utf-8") == ""
    args = (out / "args").read_text(encoding="utf-8").split()
    # The file by its absolute path (bash's own spelling of it on Windows: /tmp/...).
    given = args.pop(2)
    assert Path(given).name == secrets_file.name
    assert given.startswith("/") or given[1:3] == ":/", given
    assert args == [
        "compose",
        "--env-file",
        "-p",
        "arkray-prod",
        "-f",
        "docker-compose.yml",
        "-f",
        "infrastructure/compose.production.yml",
        "--profile",
        "app",
        "up",
        "-d",
    ]
    # From the checkout's root, where the Compose files' relative paths resolve.
    assert Path((out / "cwd").read_text(encoding="utf-8").strip()).name == ROOT.name


def test_another_project_name(tmp_path):
    result, out = _run(tmp_path, _secrets(tmp_path).as_posix(), "--project", "arkray-stage", "ps")
    assert result.returncode == 0, result.stderr
    args = (out / "args").read_text(encoding="utf-8").split()
    assert args[args.index("-p") + 1] == "arkray-stage"
    assert args[-1] == "ps"


def test_a_missing_file_or_one_in_the_checkout_is_refused(tmp_path):
    missing, out = _run(tmp_path, (tmp_path / "nowhere.env").as_posix(), "ps")
    assert missing.returncode != 0
    assert not (out / "args").exists()
    inside, out = _run(tmp_path, (ROOT / ".env.example").as_posix(), "ps")
    assert inside.returncode != 0
    assert "inside the repository" in inside.stderr
    assert not (out / "args").exists()
    usage, _ = _run(tmp_path)
    assert usage.returncode != 0
