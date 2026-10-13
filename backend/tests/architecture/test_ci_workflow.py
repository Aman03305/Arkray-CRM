"""The CI and release workflows, and the local runner that mirrors CI.

The CI image smoke test must start the backend the way production settings require.
Commit 7b6da1e made ERASURE_LEDGER_URL/KEY mandatory in production, but the smoke test's
environment in .github/workflows/ci.yml wasn't updated, so the images job failed on GitHub
(final production audit, 2026-10-11). This loads production settings with exactly the
variables that step passes.

The release pipeline (.github/workflows/release.yml, docs/deployment.md#release-process,
production deployment preparation): started by hand only, for a commit whose CI run on main
concluded `success`; it publishes only images that passed CI's own scan and smoke-test steps
(the same scripts, compared here), attests their digests, and waits for the `staging` and
`production` environments' reviewers. Every workflow pins each action to a commit with its
exact release named, each tool image by digest, grants each job only the permissions it
uses, gives every job a timeout, keeps the token out of checkouts, and never interpolates an
expression (`${{ ... }}`, user input above all) into a shell script. scripts/ci-local.sh runs
CI's jobs locally with the same pins, environment and gates.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from tests.architecture.test_production_settings import BASE_ENV, load_production_settings

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
RELEASE_WORKFLOW = WORKFLOWS / "release.yml"
CI_LOCAL = ROOT / "scripts" / "ci-local.sh"
SMOKE_STEP = "Smoke-test the images"
SCAN_STEP = "Scan the images for known vulnerabilities and secrets"

ACTION = re.compile(r"[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}")
RELEASE_COMMENT = re.compile(r"\s+# v\d+\.\d+\.\d+")
# An image a script names by registry path and version tag (`aquasec/trivy:0.70.0`), with or
# without its digest. Local tags (`arkray-backend:ci`) and URLs don't match.
VERSIONED_IMAGE = re.compile(
    r"(?<![\w/.:-])([a-z0-9][a-z0-9.-]*(?:/[a-z0-9._-]+)+:v?\d[\w.-]*)(@sha256:[0-9a-f]{64})?"
)
RELEASE_DATABASE = (
    "python manage.py migrate --noinput && python manage.py grant_app_privileges arkray_app"
    " && python manage.py migrate --check"
)


def _load(path: Path) -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return workflow


def _workflows() -> dict[str, dict[str, Any]]:
    paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    return {path.name: _load(path) for path in paths}


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the key `on` as the boolean true.
    triggers: dict[str, Any] = workflow["on"] if "on" in workflow else workflow[True]
    return triggers


def _steps(workflow: dict[str, Any], job: str | None = None) -> list[dict[str, Any]]:
    jobs = [workflow["jobs"][job]] if job else workflow["jobs"].values()
    return [step for each in jobs for step in each.get("steps", [])]


def _run(workflow: dict[str, Any], name: str, job: str | None = None) -> str:
    (script,) = [step["run"] for step in _steps(workflow, job) if step.get("name") == name]
    return str(script)


def _environment(script: str) -> dict[str, str]:
    block = re.search(r"prod_env=\((.*?)\n\s*\)", script, re.DOTALL)
    assert block, "the smoke test no longer defines prod_env=( ... )"
    environment = {}
    for name, value in re.findall(r"-e ([A-Z_][A-Z0-9_]*)=(\"[^\"]*\"|\S+)", block.group(1)):
        # `$(openssl rand -hex 32)` is a random 64-character suffix at run time.
        environment[name] = re.sub(r"\$\([^)]*\)", "0" * 64, value).strip('"')
    return environment


def _smoke_environment() -> dict[str, str]:
    return _environment(_run(_load(CI_WORKFLOW), SMOKE_STEP))


def _builds(workflow: dict[str, Any], job: str) -> list[str]:
    """The job's `docker build` commands, without the labels the release adds."""
    commands = [
        " ".join(step["run"].split())
        for step in _steps(workflow, job)
        if str(step.get("run", "")).startswith("docker build")
    ]
    return [re.sub(r' --label "[^"]*"', "", command) for command in commands]


def _pins(text: str) -> dict[str, str]:
    """Each versioned image the text names, by repository: its full pinned reference."""
    return {
        match[0].rsplit(":", 1)[0]: match[0] + match[1] for match in VERSIONED_IMAGE.findall(text)
    }


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


# --- supply chain -------------------------------------------------------------------------


def test_every_action_is_pinned_to_a_commit_named_by_its_release():
    """A tag can be moved to other code; a commit can't. The comment names the exact release
    the commit was taken from (checked with `git ls-remote --tags`), and one action is pinned
    to one commit in every workflow."""
    commits: dict[str, set[str]] = {}
    for name, workflow in _workflows().items():
        expected = sum("uses" in step for step in _steps(workflow))
        expected += sum("uses" in job for job in workflow["jobs"].values())
        lines = (WORKFLOWS / name).read_text(encoding="utf-8").splitlines()
        found = 0
        for line in lines:
            match = re.match(r"\s*(?:-\s+)?uses:\s*(\S+)(.*)$", line)
            if not match:
                continue
            found += 1
            reference, comment = match.groups()
            assert ACTION.fullmatch(reference), f"{name}: {reference} is not pinned to a commit"
            assert RELEASE_COMMENT.fullmatch(comment), f"{name}: {reference} names no release"
            action, commit = reference.split("@")
            commits.setdefault(action, set()).add(commit)
        assert found == expected, f"{name}: a `uses:` the check above didn't see"
    assert len(commits) >= 7
    assert {action: shas for action, shas in commits.items() if len(shas) > 1} == {}


def test_every_tool_image_is_pinned_by_digest():
    """Scanner, linter and database images the scripts start, in the workflows and the local
    runner: a moved tag can't change what runs."""
    texts = [path.read_text(encoding="utf-8") for path in sorted(WORKFLOWS.glob("*.y*ml"))]
    texts.append(CI_LOCAL.read_text(encoding="utf-8"))
    images = [match for text in texts for match in VERSIONED_IMAGE.findall(text)]
    named = {image for image, _ in images}
    assert {"aquasec/trivy:0.70.0", "rhysd/actionlint:1.7.12"} <= named
    assert sorted({image for image, digest in images if not digest}) == []


def test_no_workflow_runs_on_untrusted_events():
    """pull_request_target and workflow_run run with the base repository's token and secrets
    on code or artifacts a fork controls."""
    for name, workflow in _workflows().items():
        assert {"pull_request_target", "workflow_run"}.isdisjoint(_triggers(workflow)), name


def test_no_expression_is_interpolated_into_a_shell_script():
    """`${{ ... }}` is pasted into the script before the shell parses it: an input or event
    field holding `"; curl ... #` would run. Values reach scripts through `env:` only."""
    for name, workflow in _workflows().items():
        for step in _steps(workflow):
            assert "${{" not in str(step.get("run", "")), f"{name}: {step.get('name', step)}"


def test_every_job_has_a_timeout_and_no_checkout_keeps_the_token():
    for name, workflow in _workflows().items():
        for job_name, job in workflow["jobs"].items():
            assert isinstance(job.get("timeout-minutes"), int), f"{name}: {job_name}"
        for step in _steps(workflow):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False, name


def test_permissions_are_the_least_each_job_needs():
    ci = _load(CI_WORKFLOW)
    assert ci["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in ci["jobs"].values())

    release = _load(RELEASE_WORKFLOW)
    assert release["permissions"] == {}
    assert {name: job["permissions"] for name, job in release["jobs"].items()} == {
        "verify": {"actions": "read", "contents": "read"},
        "publish": {
            "contents": "read",
            "packages": "write",
            "id-token": "write",
            "attestations": "write",
        },
        "promote-production": {"contents": "read", "packages": "read", "attestations": "read"},
    }


def test_ci_finishes_every_commit_on_main():
    """A release needs CI's verdict on its exact commit: pushes to main are never cancelled
    or replaced by a newer one; a pull request's newer push replaces its running checks."""
    concurrency = _load(CI_WORKFLOW)["concurrency"]
    assert concurrency["group"] == (
        "ci-${{ github.event_name == 'pull_request' && github.ref || github.sha }}"
    )
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    release = _load(RELEASE_WORKFLOW)["concurrency"]
    assert release["cancel-in-progress"] is False


def test_ci_releases_a_new_database_with_the_image():
    """docs/deployment.md#release-process steps 2-4 with the built image: roles.sql, every
    migration from nothing as the owner (the suite migrates as a superuser), the grant,
    nothing left unapplied, the release check as the application role. The passwords never
    appear on a command line."""
    script = _run(_load(CI_WORKFLOW), SMOKE_STEP)
    assert "< infrastructure/postgres/roles.sql" in script
    assert "-e OWNER_PASSWORD -e APP_PASSWORD" in script
    assert f"sh -c '{RELEASE_DATABASE}'" in script
    assert (
        'DATABASE_URL="postgres://arkray_owner:${OWNER_PASSWORD}@127.0.0.1:5432/arkray"' in script
    )
    assert 'DATABASE_URL="postgres://arkray_app:${APP_PASSWORD}@127.0.0.1:5432/arkray"' in script
    assert script.count('"${prod_env[@]}" -e DATABASE_URL "${hardened[@]}"') == 2
    assert "python manage.py check --deploy --database default" in script
    assert "uv sync --locked" in [step.get("run") for step in _steps(_load(CI_WORKFLOW), "backend")]


# --- the release --------------------------------------------------------------------------


def test_a_release_is_started_by_hand_for_one_commit():
    triggers = _triggers(_load(RELEASE_WORKFLOW))
    assert set(triggers) == {"workflow_dispatch"}
    sha = triggers["workflow_dispatch"]["inputs"]["sha"]
    assert sha["required"] is True
    assert sha["type"] == "string"


def test_a_release_requires_ci_success_for_exactly_that_commit():
    release = _load(RELEASE_WORKFLOW)
    (step,) = _steps(release, "verify")
    assert step["env"]["REQUESTED_SHA"] == "${{ inputs.sha }}"
    script = step["run"]
    assert '[[ "$REQUESTED_SHA" =~ ^[0-9a-f]{40}$ ]]' in script
    assert '[[ "$REQUESTED_SHA" == "$GITHUB_SHA" ]]' in script
    assert "refs/heads/main | refs/tags/v*) ;;" in script
    assert 'compare/$GITHUB_SHA...main" --jq .status' in script
    assert '"repos/$GITHUB_REPOSITORY/actions/workflows/ci.yml/runs"' in script
    assert '-f head_sha="$GITHUB_SHA" -f event=push -f branch=main' in script
    assert '.head_sha == $sha and .path == ".github/workflows/ci.yml"' in script
    assert '.status == "completed" and .conclusion == "success"' in script
    assert '[[ -n "$ci_run" ]] ||' in script
    # Both approvals exist before anything is built: a missing environment would be created
    # without reviewers when a job names it.
    assert "for environment in staging production; do" in script
    assert 'select(.type == "required_reviewers")' in script
    assert '[[ "$reviewers" -ge 1 ]] ||' in script
    # Only chosen branches and tags may deploy (L14): a copy of this file on another branch
    # can drop the checks above, not the environment's own policy.
    # Exactly main and v*, by custom rules (review round 1, INFRA-8: `!= null` let
    # "protected branches" or a `*` rule through).
    assert '.custom_branch_policies and (.protected_branches | not) then "custom"' in script
    assert '[[ "$policy" == custom ]] ||' in script
    assert "/deployment-branch-policies" in script
    assert '[[ "$rules" == "branch:main tag:v*" ]] ||' in script
    assert script.rstrip().endswith('>> "$GITHUB_STEP_SUMMARY"')
    assert 'echo "sha=$GITHUB_SHA" >> "$GITHUB_OUTPUT"' in script


def test_publishing_and_promotion_wait_for_their_environments():
    jobs = _load(RELEASE_WORKFLOW)["jobs"]
    assert "environment" not in jobs["verify"]
    assert jobs["publish"]["environment"] == "staging"
    assert jobs["publish"]["needs"] == "verify"
    assert jobs["promote-production"]["environment"] == "production"
    assert set(jobs["promote-production"]["needs"]) == {"verify", "publish"}
    # Production deploys nothing and runs no repository code: no checkout, no write access.
    promote = _steps(_load(RELEASE_WORKFLOW), "promote-production")
    assert all("uses" not in step for step in promote)
    assert "write" not in jobs["promote-production"]["permissions"].values()


def test_a_release_publishes_only_images_that_passed_cis_gates():
    ci, release = _load(CI_WORKFLOW), _load(RELEASE_WORKFLOW)
    assert _run(release, SCAN_STEP, "publish") == _run(ci, SCAN_STEP, "images")
    assert _run(release, SMOKE_STEP, "publish") == _run(ci, SMOKE_STEP, "images")
    assert _builds(release, "publish") == _builds(ci, "images")
    assert len(_builds(ci, "images")) == 2
    steps = _steps(release, "publish")
    assert steps[0]["with"]["ref"] == "${{ needs.verify.outputs.sha }}"
    names = [step.get("name") or step.get("uses", "").split("@")[0] for step in steps]
    push = names.index("Push the images to GHCR and record their digests")
    assert names.index(SCAN_STEP) < push
    assert names.index(SMOKE_STEP) < push
    assert names.index("Generate the SBOMs") < push
    attestations = [step for step in steps[push + 1 :] if "uses" in step]
    assert [step["uses"].split("@")[0] for step in attestations] == [
        "actions/attest-build-provenance",
        "actions/attest-build-provenance",
        "actions/attest-sbom",
        "actions/attest-sbom",
    ]
    subjects = set()
    for step in attestations:
        assert step["with"]["push-to-registry"] is True
        name, digest = step["with"]["subject-name"], step["with"]["subject-digest"]
        image = re.fullmatch(r"\$\{\{ steps\.push\.outputs\.(backend|frontend)_name \}\}", name)
        assert image
        assert digest == name.replace("_name", "_digest")
        if step["uses"].startswith("actions/attest-sbom@"):
            # Each image's own SBOM, as the SBOM step writes it.
            sbom = f"${{{{ runner.temp }}}}/sbom/arkray-{image.group(1)}.cdx.json"
            assert step["with"]["sbom-path"] == sbom
        assert set(step["with"]) <= {
            "subject-name",
            "subject-digest",
            "sbom-path",
            "push-to-registry",
        }
        subjects.add((step["uses"].split("@")[0], name))
    assert len(subjects) == 4
    sboms = _run(release, "Generate the SBOMs", "publish")
    assert "for name in backend frontend; do" in sboms
    assert '--format cyclonedx --output "/sbom/arkray-$name.cdx.json" "arkray-$name:ci"' in sboms
    script = _run(release, "Push the images to GHCR and record their digests", "publish")
    assert "is published already" in script
    assert 'docker push "$repository:$RELEASE_SHA"' in script


def test_production_verifies_the_attestations_of_the_exact_digests():
    script = _run(
        _load(RELEASE_WORKFLOW), _steps(_load(RELEASE_WORKFLOW), "promote-production")[0]["name"]
    )
    assert "^ghcr\\.io/$owner/arkray-(backend|frontend)@sha256:[0-9a-f]{64}$" in script
    assert '--signer-workflow "$GITHUB_REPOSITORY/.github/workflows/release.yml"' in script
    # Bound to this run's exact ref, which verify allowed (adversarial review of 73ff1e3, L14).
    assert (
        '--source-digest "$RELEASE_SHA" --source-ref "$GITHUB_REF" --deny-self-hosted-runners'
        in script
    )
    assert script.index("refs/heads/main | refs/tags/v*) ;;") < script.index("verify=(")
    assert 'gh attestation verify "oci://$image" "${verify[@]}"\n' in script
    assert '"${verify[@]}" --predicate-type https://cyclonedx.org/bom' in script
    assert 'echo "docker pull $image"' in script


# --- the local runner ---------------------------------------------------------------------


def test_the_local_runner_runs_cis_gates_with_cis_pins_and_environment():
    ci_text = CI_WORKFLOW.read_text(encoding="utf-8")
    ci = _load(CI_WORKFLOW)
    local = CI_LOCAL.read_text(encoding="utf-8")
    # The same tools, by digest (and gitleaks at CI's version).
    local_pins, ci_pins = _pins(local), _pins(ci_text)
    for repository in ("aquasec/trivy", "rhysd/actionlint", "pgvector/pgvector"):
        assert local_pins[repository] == ci_pins[repository]
    secrets = _run(ci, "Scan the history for secrets")
    (version,) = re.findall(r"^\s*version=(\S+)$", secrets, re.M)
    assert local_pins["ghcr.io/gitleaks/gitleaks"].startswith(
        f"ghcr.io/gitleaks/gitleaks:v{version}@"
    )
    # CI's history scan with CI's flags (values redacted), then the same flags for the rest.
    (flags,) = re.findall(r"^\s*/tmp/gitleaks git (.*) \.$", secrets, re.M)
    assert "--redact" in flags.split()
    assert f'"$GITLEAKS_IMAGE" git {flags} "$@" /repo' in local
    assert f'"$GITLEAKS_IMAGE" dir {flags} /scan' in local
    # The same smoke-test environment, hardening, gates and release on a new database.
    assert _environment(local) == _smoke_environment()
    hardened = (
        "hardened=(--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true)"
    )
    assert hardened in local
    assert hardened in _run(ci, SMOKE_STEP)
    scan = _run(ci, SCAN_STEP)
    for gate in (
        '--scanners vuln --severity HIGH,CRITICAL "$image"',
        '--scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 "$image"',
        '--scanners secret --exit-code 1 "$image"',
    ):
        assert gate in scan
        assert gate in local
    assert "--build-arg API_ORIGIN=http://backend:8000" in local
    assert f"sh -c '{RELEASE_DATABASE}'" in local
    assert "python manage.py check --deploy --database default" in local
    assert '"$src/infrastructure/postgres/roles.sql"' in local
    assert "scripts/check.sh backend" in local
    assert "scripts/check.sh frontend" in local


# --- development credentials never reach the production stack (adversarial review M4) ------
COMPOSE_STEP = "Refuse development credentials in the production stack"
GUARD_STEP = "Check the credential guard in the image"
REQUIRED_SECRETS = re.compile(r"grep -oE '(.+?)' \| sed -E '(.+?)' \| sort -u\)")


def test_ci_refuses_the_production_stack_with_the_development_env_file():
    """For each secret the overlay requires, CI leaves it out, with `.env.example` copied to
    `.env`, and Compose must refuse naming it; the local runner does the same with
    `--env-file .env.example` (a developer's own `.env` untouched)."""
    ci = _load(CI_WORKFLOW)
    job = ci["jobs"]["compose"]
    assert isinstance(job["timeout-minutes"], int)
    script = _run(ci, COMPOSE_STEP, "compose")
    assert script.index("cp .env.example .env") < script.index("docker compose")
    for line in (
        "-f docker-compose.yml -f infrastructure/compose.production.yml --profile app",
        'docker compose "${files[@]}" config --quiet',
        'if (configure "$missing") 2> "$RUNNER_TEMP/compose.err"; then',
        'grep -q "$missing" "$RUNNER_TEMP/compose.err"',
        '(configure "")',
    ):
        assert line in script, line
    local = CI_LOCAL.read_text(encoding="utf-8")
    assert "JOBS=(secrets workflows compose backend frontend images)" in local
    assert 'docker compose --env-file .env.example "${files[@]}" config --quiet' in local
    assert "-f docker-compose.yml -f infrastructure/compose.production.yml --profile app" in local
    assert 'grep -q "$missing" "$errors"' in local
    # The same extraction of the required names in both.
    in_ci, in_local = REQUIRED_SECRETS.search(script), REQUIRED_SECRETS.search(local)
    assert in_ci
    assert in_local
    assert in_ci.groups() == in_local.groups()


def test_ci_checks_the_credential_guard_in_the_image():
    from config.credentials import COMPOSE_SECRETS

    script = _run(_load(CI_WORKFLOW), GUARD_STEP, "images")
    names = set(re.findall(r"\bARKRAY_PROD_[A-Z_]+\b", script.split("guard=(")[0]))
    assert names == {secret.name for secret in COMPOSE_SECRETS if secret.required}
    assert "--network none --read-only --cap-drop ALL" in script
    assert script.count("arkray-backend:ci python -m config.credentials") == 2
    assert "export ARKRAY_PROD_DB_SUPERUSER_PASSWORD=arkray_dev_only_password" in script
    local = CI_LOCAL.read_text(encoding="utf-8")
    assert (
        'gate "images: the credential guard refuses development values" credential_guard' in local
    )
    assert local.count('"$BACKEND_IMAGE" python -m config.credentials') == 2


# --- Dependabot (adversarial review of 73ff1e3, L14) ----------------------------------------
def test_dependabot_covers_every_ecosystem_weekly_and_grouped():
    """docs/security.md named Dependabot; nothing configured it. Every dependency the
    repository pins: Python (uv), the frontend (npm/pnpm), the Dockerfiles' and Compose
    files' images, the workflows' actions."""
    config = _load(ROOT / ".github" / "dependabot.yml")
    assert config["version"] == 2
    found = {}
    for update in config["updates"]:
        directories = update.get("directories") or [update["directory"]]
        found[update["package-ecosystem"]] = directories
        assert update["schedule"]["interval"] == "weekly", update["package-ecosystem"]
        assert update["groups"], update["package-ecosystem"]
        assert update["cooldown"]["default-days"] >= 3, update["package-ecosystem"]
        for group in update["groups"].values():
            assert group["applies-to"] == "version-updates"
            assert group["patterns"] == ["*"]
        for directory in directories:
            assert (ROOT / directory.lstrip("/")).is_dir(), directory
    assert found == {
        "uv": ["/backend"],
        "npm": ["/frontend"],
        "docker": ["/backend", "/frontend"],
        "docker-compose": ["/", "/infrastructure"],
        "github-actions": ["/"],
    }
    assert (ROOT / "backend" / "uv.lock").is_file()
    assert (ROOT / "frontend" / "pnpm-lock.yaml").is_file()
    # Each docker-compose directory holds a file Dependabot recognises by name (its
    # FILENAME_REGEX, matched anywhere in the name; review round 1, INFRA-9).
    compose_name = re.compile(r"(docker-)?compose(-[\w]+)?(?>\.[\w-]+)?\.ya?ml", re.I)
    for directory in found["docker-compose"]:
        names = [path.name for path in (ROOT / directory.lstrip("/")).iterdir() if path.is_file()]
        assert [name for name in names if compose_name.search(name)], directory
