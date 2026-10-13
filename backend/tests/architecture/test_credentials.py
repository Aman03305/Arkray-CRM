"""The one rule a production credential must pass (config/credentials.py), and where it is
enforced (adversarial review of 73ff1e3, M4).

The production-shaped stack started with the PostgreSQL superuser's password published in
`.env.example`, and nothing anywhere checked a database or Redis password. The rule now lives
in one place and runs three times:

- the production stack's one-shot guard (`python -m config.credentials`, the
  `credential-guard` service every other one waits for) refuses a missing secret, a known
  development or placeholder value, a weak one and the same value used twice, naming the
  variable and never printing a value;
- production settings refuse a development or placeholder password in DATABASE_URL and the
  Redis URLs (the local stack's explicit opt-in aside, which is refused for public names);
- `manage.py preflight` judges those passwords by the whole rule (FAIL).

Every development value the repository publishes (`.env.example`, the development Compose
file's defaults, CI's database service, the test settings' default database URL) and every
placeholder of the production template is refused; generated values pass.
"""

from __future__ import annotations

import os
import random
import re
import secrets
import string
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from config import credentials
from config.preflight import FAIL, PASS, SKIP, judge_credential
from tests.architecture.test_production_settings import LOCAL_OPT_IN, load_production_settings

BACKEND = Path(__file__).resolve().parents[2]
ROOT = BACKEND.parent
SECRET_NAME = re.compile(r"(?:PASSWORD|SECRET_KEY|LEDGER_KEY|TOKEN)$")
URL_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^:/@]*:([^@]+)@")


def published_development_values() -> set[str]:
    """Every secret value the repository itself publishes for development or tests."""
    values: set[str] = set()
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name, value in re.findall(r"^([A-Z][A-Z0-9_]*)=(.+)$", example, re.M):
        if SECRET_NAME.search(name):
            values.add(value)
        if password := URL_PASSWORD.match(value):
            values.add(password.group(1))
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for name, value in re.findall(r"\$\{([A-Z][A-Z0-9_]*):-([^}]+)\}", compose):
        if SECRET_NAME.search(name):
            values.add(value)
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    for job in ci["jobs"].values():
        for service in (job.get("services") or {}).values():
            values |= {v for k, v in (service.get("env") or {}).items() if SECRET_NAME.search(k)}
    test_settings = (BACKEND / "config" / "settings" / "test.py").read_text(encoding="utf-8")
    for url in re.findall(r'"(postgres://[^"]+)"', test_settings):
        values.add(urlsplit(url).password or "")
    values.discard("")
    return values


def template_placeholders() -> set[str]:
    template = ROOT / "infrastructure" / "env" / "production.env.example"
    return set(re.findall(r"change-me-[a-z0-9-]+", template.read_text(encoding="utf-8")))


def test_the_scan_finds_the_published_values():
    found = published_development_values()
    assert "arkray_dev_only_password" in found
    assert any(value.startswith("dev-only-insecure-key") for value in found)
    assert any(value.startswith("dev-only-erasure-ledger-key") for value in found)
    assert len(template_placeholders()) >= 10


@pytest.mark.parametrize(
    "value", sorted(published_development_values() | template_placeholders()), ids=repr
)
def test_every_published_value_is_refused(value):
    assert credentials.is_development_value(value)
    assert credentials.weakness(value) is not None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "x",
        "p" * 31,  # one short of the minimum
        "Zx9-Qm4_Lp7.Rt2~Vb8Kc5W3nF6hJ1d",  # random-looking, 31 characters: the length alone
        "a" * 64,  # long, one character
        "abcabcabcabcabcabcabcabcabcabcab",  # 32 characters, 3 distinct
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaabcdefg",  # 7 distinct, nearly all one
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaabcdefghijk",  # 11 distinct, under 2.5 bits a character
        "12345678901234567890123456789012345",  # digits only (and a sequence)
        "Arkray-Production-2026-Password!-Main",  # a marker word
        "test-" + secrets.token_hex(24),  # a test prefix
        "dev_" + secrets.token_hex(24),
        # Review round 1 (INFRA-4): guessable values the first rule accepted, and the same
        # shapes at the new length.
        "arkray-crm-production-2026",
        "123456789012345678901234567890",
        "Summer2026Summer2026Summer2026",
        "Welcome1Welcome1Welcome1",
        "Passw0rdPassw0rdPassw0rd",
        "arkray-crm-production-2026-secret-0001",  # words and numbers joined
        "Arkray_Prod_Superuser_Main_Database_2026",
        "Summer2026Summer2026Summer2026Summer2026",  # one unit repeated
        "Welcome1Welcome1Welcome1Welcome1",
        "Passw0rdPassw0rdPassw0rdPassw0rd",
        "Zq81Lr0xVt2PZq81Lr0xVt2PZq81Lr0xVt2P",  # a random-looking unit, repeated
        "12345678901234567890123456789012",  # a sequence
        "qwertyuiopasdfghjklzxcvbnm1234567890",  # keyboard rows
        "9876543210zyxwvutsrqponmlkjihgfedcba",  # descending
        "Xk7pR2mWelcomeToArkray9vQ4tZ8nL3sB",  # a common word inside random-looking text
        "4hT9qL2xSunshine8Vm3Kp7Rz1Wn5Yb6Jd",
        "ci-image-smoke-ledger-" + "0" * 64,  # a long run of one character
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaab1C2d3E4",  # varied, but nearly all one character
        "orange-violet-copper-meadow-4821",  # words and a number joined, no common word
        "Granite_Harbor_Lantern_Quiet_7733",
    ],
    ids=repr,
)
def test_weak_values_are_refused(value):
    assert credentials.weakness(value) is not None


def test_a_whole_file_of_guessable_values_is_refused():
    """The reviewer's file: every secret `arkray-crm-production-2026-<n>`, and the same at
    the new length; the guard refuses each one."""
    for suffix in ("", "-extended-for-length"):
        environ = {
            secret.name: f"arkray-crm-production-2026{suffix}-{index}"
            for index, secret in enumerate(credentials.COMPOSE_SECRETS)
            if secret.required
        }
        problems = credentials.check_environment(environ)
        assert len(problems) == len(environ), problems


@pytest.mark.parametrize(
    "make",
    [
        lambda: secrets.token_hex(16),  # 32 hex characters: openssl rand -hex 16
        lambda: secrets.token_hex(32),  # scripts/init-prod-env.sh's
        lambda: secrets.token_urlsafe(24),  # 32 characters
        lambda: secrets.token_urlsafe(64),
    ],
)
def test_generated_values_pass(make):
    for _ in range(50):
        value = make()
        assert credentials.weakness(value) is None, len(value)


def test_generated_values_pass_in_bulk():
    """Every check is rare on random values (measured: none of 200,000 of each kind refused
    at these thresholds); a fixed seed keeps this deterministic."""
    rng = random.Random(73)
    hexadecimal = "0123456789abcdef"
    urlsafe = string.ascii_letters + string.digits + "-_"
    for _ in range(5_000):
        for alphabet, length in (
            (hexadecimal, 32),
            (hexadecimal, 64),
            (urlsafe, 32),
            (urlsafe, 43),
        ):
            value = "".join(rng.choice(alphabet) for _ in range(length))
            assert credentials.weakness(value) is None, value


def test_the_rule_never_quotes_the_value():
    for value in ("arkray_dev_only_password", "short-secret", "a" * 40):
        reason = credentials.weakness(value)
        assert reason is not None
        assert value not in reason


def _generated() -> dict[str, str]:
    return {
        secret.name: secrets.token_hex(32)
        for secret in credentials.COMPOSE_SECRETS
        if secret.required
    }


def test_the_guard_accepts_generated_secrets():
    assert credentials.check_environment(_generated()) == []


def test_the_guard_refuses_missing_development_weak_unsafe_and_reused_secrets():
    names = [secret.name for secret in credentials.COMPOSE_SECRETS if secret.required]
    for name in names:
        environ = _generated()
        del environ[name]
        assert any(name in problem for problem in credentials.check_environment(environ))
    for value in ("arkray_dev_only_password", "change-me-app-password", "s3cret", "a" * 30):
        environ = {**_generated(), "ARKRAY_PROD_DB_APP_PASSWORD": value}
        problems = credentials.check_environment(environ)
        assert len(problems) == 1, problems
        assert "ARKRAY_PROD_DB_APP_PASSWORD" in problems[0]
        assert value not in problems[0]
    # A password inside a connection URL: unreserved characters only.
    unsafe = secrets.token_hex(16) + "@:/?#"
    problems = credentials.check_environment({**_generated(), "ARKRAY_PROD_REDIS_PASSWORD": unsafe})
    assert [p for p in problems if "ARKRAY_PROD_REDIS_PASSWORD" in p]
    assert all(unsafe not in problem for problem in problems)
    # The same value twice: one leak would open both.
    environ = _generated()
    environ["ARKRAY_PROD_DB_APP_PASSWORD"] = environ["ARKRAY_PROD_DB_SUPERUSER_PASSWORD"]
    problems = credentials.check_environment(environ)
    assert any(
        "ARKRAY_PROD_DB_APP_PASSWORD" in p and "ARKRAY_PROD_DB_SUPERUSER_PASSWORD" in p
        for p in problems
    )
    # The optional provider key is checked only when it is given.
    assert (
        credentials.check_environment({**_generated(), "ARKRAY_PROD_ANTHROPIC_API_KEY": ""}) == []
    )
    assert credentials.check_environment(
        {**_generated(), "ARKRAY_PROD_ANTHROPIC_API_KEY": "change-me-anthropic-api-key"}
    )


def _guard(environ: dict[str, str]) -> subprocess.CompletedProcess[str]:
    keep = {k: v for k, v in os.environ.items() if not k.startswith("ARKRAY_PROD_")}
    return subprocess.run(
        [sys.executable, "-m", "config.credentials"],
        cwd=BACKEND,
        env={**keep, **environ},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_the_guard_command_exits_non_zero_and_names_only_the_variables():
    environ = {**_generated(), "ARKRAY_PROD_DB_SUPERUSER_PASSWORD": "arkray_dev_only_password"}
    result = _guard(environ)
    assert result.returncode == 1
    assert "ARKRAY_PROD_DB_SUPERUSER_PASSWORD" in result.stderr
    for value in environ.values():
        assert value not in result.stdout + result.stderr
    accepted = _guard(_generated())
    assert accepted.returncode == 0, accepted.stderr
    for value in _generated().values():
        assert value not in accepted.stdout + accepted.stderr


# --- production settings ---------------------------------------------------------------------
DEVELOPMENT_URLS = {
    "DATABASE_URL": "postgres://arkray:arkray_dev_only_password@localhost:5432/arkray",
    "CELERY_BROKER_URL": (
        "rediss://:arkray_dev_only_password@redis.internal:6380/0?ssl_cert_reqs=required"
    ),
    "REDIS_CACHE_URL": (
        "rediss://:change-me-cache-password@redis.internal:6380/1?ssl_cert_reqs=required"
    ),
}


@pytest.mark.parametrize("setting", sorted(DEVELOPMENT_URLS))
def test_production_settings_refuse_a_development_password(setting):
    result = load_production_settings(**{setting: DEVELOPMENT_URLS[setting]})
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert setting in result.stderr
    password = urlsplit(DEVELOPMENT_URLS[setting]).password or ""
    assert password not in result.stderr


def test_the_local_stack_keeps_its_development_database_password():
    """docker-compose.yml's app profile: production settings with the local opt-in, the
    published password and local names only."""
    result = load_production_settings(
        **LOCAL_OPT_IN,
        DATABASE_URL="postgres://arkray:arkray_dev_only_password@postgres:5432/arkray",
        CELERY_BROKER_URL="redis://redis:6379/0",
        REDIS_CACHE_URL="redis://redis:6379/1",
        DJANGO_SECURE_COOKIES="false",
        DJANGO_SECURE_SSL_REDIRECT="false",
        DJANGO_HSTS_SECONDS="0",
        APP_BASE_URL="http://localhost:3000",
        DJANGO_CSRF_TRUSTED_ORIGINS="http://localhost:3000",
        EMAIL_URL="smtp://mailpit:1025",
        DB_REQUIRE_RESTRICTED_ROLE="false",
    )
    assert result.returncode == 0, result.stderr[-2000:]


# --- preflight --------------------------------------------------------------------------------
def test_preflight_judges_each_password_by_the_whole_rule():
    strong = secrets.token_hex(32)
    assert judge_credential("credentials.database", "DATABASE_URL", strong).status == PASS
    for weak in ("arkray_dev_only_password", "Zq7", "a" * 40):
        result = judge_credential("credentials.database", "DATABASE_URL", weak)
        assert result.status == FAIL
        assert weak not in result.detail
        assert "DATABASE_URL" in result.detail
    # No password in the URL: the server authenticates another way (a client certificate).
    assert judge_credential("credentials.database", "DATABASE_URL", "").status == SKIP
    # Redis always needs one.
    assert (
        judge_credential("credentials.broker", "CELERY_BROKER_URL", "", required=True).status
        == FAIL
    )


# --- the provisioning script and the generator -----------------------------------------------
def test_roles_sql_refuses_short_and_shared_role_passwords():
    """infrastructure/postgres/roles.sql runs where no guard does (a managed database): it
    refuses role passwords under the rule's minimum length, or one for both roles, before
    any role is created, and stores SCRAM verifiers."""
    roles = (ROOT / "infrastructure" / "postgres" / "roles.sql").read_text(encoding="utf-8")
    (length_check,) = re.findall(r"^SELECT length\(.*?\\gset$", roles, re.M | re.S)
    assert re.findall(r">= (\d+)", length_check) == [str(credentials.MIN_LENGTH)] * 2
    assert ":'owner_password' <> :'app_password'" in length_check
    assert roles.index("\\gset") < roles.index("SELECT format('CREATE ROLE")
    assert roles.index("set_config('log_statement") < roles.index("SELECT length(")
    assert "SET password_encryption = 'scram-sha-256';" in roles


def test_the_generator_writes_every_secret_the_guard_checks():
    script = (ROOT / "scripts" / "init-prod-env.sh").read_text(encoding="utf-8")
    generated = set(re.findall(r"\b(ARKRAY_PROD_[A-Z_]+)\b", script))
    assert generated == {secret.name for secret in credentials.COMPOSE_SECRETS}
    assert "umask 077" in script
    assert 'chmod 600 "$target"' in script


BASH = next(
    (
        str(candidate)
        for candidate in (Path(r"C:\Program Files\Git\bin\bash.exe"), Path("/bin/bash"))
        if candidate.exists()
    ),
    None,
)


@pytest.mark.skipif(
    BASH is None or not __import__("shutil").which("openssl"), reason="bash+openssl"
)
def test_a_generated_file_passes_the_guard_and_stays_out_of_the_repository(tmp_path):
    assert BASH is not None
    script = (ROOT / "scripts" / "init-prod-env.sh").as_posix()
    target = tmp_path / "arkray-prod.env"
    made = subprocess.run(  # noqa: S603 — fixed program and arguments
        [BASH, script, target.as_posix()], capture_output=True, text=True, check=False, timeout=60
    )
    assert made.returncode == 0, made.stderr
    values = dict(
        line.split("=", 1)
        for line in target.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert credentials.check_environment(values) == []
    for value in values.values():
        if value:
            assert value not in made.stdout + made.stderr
    # Never twice, never inside the checkout.
    again = subprocess.run(  # noqa: S603
        [BASH, script, target.as_posix()], capture_output=True, text=True, check=False, timeout=60
    )
    assert again.returncode != 0
    inside = subprocess.run(  # noqa: S603
        [BASH, script, (ROOT / "infrastructure" / "env" / "never.env").as_posix()],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert inside.returncode != 0
    assert "inside the repository" in inside.stderr
    assert not (ROOT / "infrastructure" / "env" / "never.env").exists()
