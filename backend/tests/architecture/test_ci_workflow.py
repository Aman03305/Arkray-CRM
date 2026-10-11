"""The CI image smoke test must start the backend the way production settings require.

Commit 7b6da1e made ERASURE_LEDGER_URL/KEY mandatory in production, but the smoke test's
environment in .github/workflows/ci.yml wasn't updated, so the images job failed on GitHub
(final production audit, 2026-10-11). This loads production settings with exactly the
variables that step passes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.architecture.test_production_settings import BASE_ENV, load_production_settings

CI_WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"
SMOKE_STEP = "Smoke-test the images"


def _smoke_environment() -> dict[str, str]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    (script,) = [step["run"] for step in steps if step.get("name") == SMOKE_STEP]
    block = re.search(r"prod_env=\((.*?)\n\s*\)", script, re.DOTALL)
    assert block, "the smoke step no longer defines prod_env=( ... )"
    environment = {}
    for name, value in re.findall(r"-e ([A-Z_][A-Z0-9_]*)=(\"[^\"]*\"|\S+)", block.group(1)):
        # `$(openssl rand -hex 32)` is a random 64-character suffix at run time.
        environment[name] = re.sub(r"\$\([^)]*\)", "0" * 64, value).strip('"')
    return environment


def test_smoke_environment_is_parsed():
    environment = _smoke_environment()
    assert {"DJANGO_SECRET_KEY", "DATABASE_URL", "ERASURE_LEDGER_URL"} <= environment.keys()


def test_production_settings_accept_the_smoke_environment():
    environment = _smoke_environment()
    # Only what the CI step passes: unset the test suite's own baseline.
    overrides: dict[str, str | None] = dict.fromkeys(BASE_ENV)
    overrides.update(environment)
    result = load_production_settings("settings.DEBUG", **overrides)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split() == ["False"]
