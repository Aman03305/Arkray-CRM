"""The production-shaped stack can never run on development credentials, and the database
answers its privileged roles only where they belong (adversarial review of 73ff1e3, M4).

infrastructure/compose.production.yml took the PostgreSQL superuser's password from
`${POSTGRES_PASSWORD:?}`. Compose fills that from the shell *and from the repository's
`.env`*, and `.env.example` (so every developer's `.env`) sets it to the public development
default; docs/operations.md sourced the operator's secrets file without `set -a`, so what it
held wasn't exported and Compose fell back to the `.env`. The stack then started with the
published superuser password, reachable from every container (no `pg_hba.conf` of its own:
the image's `host all all all scram-sha-256`), and nothing checked any password's strength.
Now:

- every secret the overlay reads has a production-only name (`ARKRAY_PROD_*`) that no
  development file defines, and nothing in the development file is required for production;
  checked here on the files and, with Docker, by `docker compose config` itself with
  `.env.example` as the environment file (CI does the same with a copied `.env`);
- a one-shot guard (`credential-guard`, `python -m config.credentials`) runs before anything
  else and every service waits for it: development, placeholder, weak and reused values are
  refused (tests/architecture/test_credentials.py has the rule);
- PostgreSQL reads the overlay's own `pg_hba.conf`: the superuser only on the local socket
  (shared with the one-shot role setup alone), `arkray_owner` only from `db-owner` (an
  internal network with a fixed subnet that only the migrate job joins), `arkray_app` over TLS
  from the networks the server is on, SCRAM everywhere, everything else rejected. Checked
  live with a throwaway stack: an application container is refused as the superuser and as
  the owner and connects as `arkray_app`; migrate connects as the owner.
"""

from __future__ import annotations

import ipaddress
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.architecture.test_compose_hardening import merged
from tests.architecture.test_compose_production import BASE, PRODUCTION, ROOT, environment, load

ENV_EXAMPLE = ROOT / ".env.example"
PG_HBA = ROOT / "infrastructure" / "postgres" / "pg_hba.conf"
PRODUCTION_ONLY = "ARKRAY_PROD_"
GUARD = "credential-guard"
OWNER_NETWORK = "db-owner"
SOCKET_VOLUME = "postgres-socket"
SOCKET_DIR = "/var/run/postgresql"
HBA_FILE = "/etc/postgresql/pg_hba.conf"
SUPERUSER, OWNER, APP = "arkray", "arkray_owner", "arkray_app"
# The database a dump is restored into before the swap (docs/runbooks.md#restore-from-backup,
# scripts/restore.sh's example, the production template's [restore] line); review round 1,
# INFRA-1: pg_hba.conf admitted the roles to `arkray` alone and the documented restore failed.
RESTORE_DATABASE = "arkray_restored"
REPLICATION = "replication"
RUNBOOKS = ROOT / "docs" / "runbooks.md"

# `${NAME}`, `${NAME:-default}`, `${NAME:?message}` and `$NAME`; `$$` is a literal dollar.
INTERPOLATION = re.compile(
    r"(?<!\$)\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])[^}]*)?\}|([A-Za-z_][A-Za-z0-9_]*))"
)
# The settings that carry a secret into a container (a password, a key, a token, or a URL
# with a password in it).
SECRET_SETTINGS = {
    "DJANGO_SECRET_KEY",
    "DATABASE_URL",
    "CELERY_BROKER_URL",
    "REDIS_CACHE_URL",
    "METRICS_TOKEN",
    "ERASURE_LEDGER_KEY",
    "ANTHROPIC_API_KEY",
    "POSTGRES_PASSWORD",
    "PGPASSWORD",
    "OWNER_PASSWORD",
    "APP_PASSWORD",
    "REDIS_PASSWORD",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def interpolated(text: str) -> dict[str, set[str]]:
    """Each variable a Compose file interpolates -> how (`:?`, `:-`, `?`, `-`, `+`, or ""
    for a plain reference). Comment lines are skipped (Compose never reads them)."""
    found: dict[str, set[str]] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in INTERPOLATION.finditer(line):
            name = match.group(1) or match.group(3)
            found.setdefault(name, set()).add(match.group(2) or "")
    return found


def dotenv_names(path: Path) -> tuple[set[str], set[str]]:
    """(assigned, commented out) variable names of an environment file."""
    assigned, commented = set(), set()
    for line in _text(path).splitlines():
        if found := re.match(r"([A-Z][A-Z0-9_]*)=", line):
            assigned.add(found.group(1))
        elif found := re.match(r"#\s*([A-Z][A-Z0-9_]*)=", line):
            commented.add(found.group(1))
    return assigned, commented


def production_names() -> set[str]:
    return set(interpolated(_text(PRODUCTION)))


# --- production-only names ---------------------------------------------------------------------
def test_every_secret_the_overlay_reads_has_a_production_only_name():
    names = production_names()
    assert names, "the overlay interpolates nothing"
    assert sorted(name for name in names if not name.startswith(PRODUCTION_ONLY)) == []
    assigned, commented = dotenv_names(ENV_EXAMPLE)
    # Not even commented out: a developer who uncomments a line would satisfy it again.
    assert sorted(names & (assigned | commented)) == []
    assert not [name for name in assigned | commented if name.startswith(PRODUCTION_ONLY)]


def test_the_development_env_file_satisfies_nothing_production_requires():
    """Compose interpolates both files whole before merging them, so a `${NAME:?}` in the
    development file is required by the production command too: none may be left there, and
    none of the overlay's may be one `.env.example` defines."""
    assigned, _ = dotenv_names(ENV_EXAMPLE)
    base_required = {n for n, how in interpolated(_text(BASE)).items() if ":?" in how}
    assert base_required == set()
    required = {n for n, how in interpolated(_text(PRODUCTION)).items() if ":?" in how}
    assert len(required) >= 7
    assert sorted(required & assigned) == []


def test_every_secret_a_production_container_holds_comes_from_a_production_only_name():
    problems = []
    for name, service in sorted(merged().items()):
        env = environment(service)
        for key in sorted(SECRET_SETTINGS & env.keys()):
            found = interpolated(env[key])
            if not found and key != "ANTHROPIC_API_KEY":
                problems.append(f"{name}.{key}: a literal value")
            for variable, how in found.items():
                if not variable.startswith(PRODUCTION_ONLY):
                    problems.append(f"{name}.{key}: from {variable}")
                elif key != "ANTHROPIC_API_KEY" and how != {":?"}:
                    problems.append(f"{name}.{key}: {variable} isn't required")
    assert problems == []


def _hermetic_environment(drop: set[str]) -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key.upper() not in drop}


DOCKER = shutil.which("docker")


@pytest.mark.skipif(DOCKER is None, reason="needs docker (compose config only: no container)")
def test_compose_refuses_the_production_stack_with_the_development_env_file():
    """`docker compose config` with `.env.example` as the environment file (what a developer's
    `.env` is) and every production secret given but one, for each one: Compose must refuse,
    naming it. With all of them it is a valid configuration (so the refusal is the missing
    name's, not a broken file)."""
    assert DOCKER is not None
    required = sorted(n for n, how in interpolated(_text(PRODUCTION)).items() if ":?" in how)
    every = set(interpolated(_text(PRODUCTION))) | set(interpolated(_text(BASE)))
    command = [
        DOCKER,
        "compose",
        "--env-file",
        ".env.example",
        "-p",
        "arkray-production-config-check",
        "-f",
        "docker-compose.yml",
        "-f",
        "infrastructure/compose.production.yml",
        "--profile",
        "app",
        "config",
        "--quiet",
    ]
    given = {name: secrets.token_hex(32) for name in required}

    def config(values: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — fixed program and arguments
            command,
            cwd=ROOT,
            env={**_hermetic_environment({n.upper() for n in every}), **values},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    accepted = config(given)
    assert accepted.returncode == 0, accepted.stderr[-2000:]
    filled_by_the_development_file = []
    for name in required:
        result = config({k: v for k, v in given.items() if k != name})
        if result.returncode == 0 or name not in result.stderr:
            filled_by_the_development_file.append(name)
    assert filled_by_the_development_file == []


# --- the guard -----------------------------------------------------------------------------------
def test_a_guard_checks_every_production_secret_before_anything_starts():
    services = merged()
    guard = services[GUARD]
    assert guard["command"] == ["python", "-m", "config.credentials"]
    assert guard.get("network_mode") == "none"  # holds every secret: no way out
    names = production_names()
    guard_env = environment(guard)
    # Exactly the overlay's secrets, each under its own name, nothing else.
    assert set(guard_env) == names
    for name, value in guard_env.items():
        assert set(interpolated(value)) == {name}, name
    from config.credentials import COMPOSE_SECRETS

    assert {secret.name for secret in COMPOSE_SECRETS} == names
    waiting = []
    for name, service in sorted(services.items()):
        if name == GUARD:
            continue
        depends = service.get("depends_on") or {}
        condition = depends.get(GUARD, {}).get("condition") if isinstance(depends, dict) else None
        if condition != "service_completed_successfully":
            waiting.append(name)
    assert waiting == [], "every service waits for the guard to succeed"


# --- pg_hba.conf ---------------------------------------------------------------------------------
def hba_rules() -> list[list[str]]:
    rules = []
    for line in _text(PG_HBA).splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            rules.append(line.split())
    return rules


def owner_subnet() -> ipaddress.IPv4Network:
    network = load(PRODUCTION)["networks"][OWNER_NETWORK]
    return ipaddress.ip_network(network["ipam"]["config"][0]["subnet"])  # type: ignore[return-value]


# A stand-in for `default` (Docker picks its subnet): the server's own networks are what
# `samenet` matches.
DEFAULT_NETWORK = ipaddress.ip_network("172.30.0.0/16")


def decide(kind: str, database: str, user: str, address: str = "") -> str:
    """The method PostgreSQL applies to a connection: the first rule that matches it
    (`kind`: local, hostssl or hostnossl), or reject when none does."""
    server_networks = [DEFAULT_NETWORK, owner_subnet()]
    for rule in hba_rules():
        rule_type = rule[0]
        if rule_type == "local":
            if kind != "local":
                continue
            databases, users, method = rule[1], rule[2], rule[3]
        else:
            if kind == "local" or (rule_type in {"hostssl", "hostnossl"} and rule_type != kind):
                continue
            databases, users, where, method = rule[1], rule[2], rule[3], rule[4]
            client = ipaddress.ip_address(address)
            if where == "samenet":
                if not any(client in network for network in server_networks):
                    continue
            elif where != "all" and client not in ipaddress.ip_network(where):
                continue
        # `all` never matches a physical replication connection (database "replication"):
        # only the keyword `replication` does (PostgreSQL's rule).
        if database == REPLICATION:
            if REPLICATION not in databases.split(","):
                continue
        elif databases != "all" and database not in databases.split(","):
            continue
        if users != "all" and user not in users.split(","):
            continue
        return method
    return "reject"


def test_the_database_answers_each_role_only_where_it_belongs():
    application = str(DEFAULT_NETWORK.network_address + 5)  # backend, a worker, beat
    migrate = str(owner_subnet().network_address + 2)
    scram = "scram-sha-256"
    # From an application container: only arkray_app, only over TLS.
    assert decide("hostssl", "arkray", SUPERUSER, application) == "reject"
    assert decide("hostssl", "postgres", SUPERUSER, application) == "reject"
    assert decide("hostssl", "arkray", OWNER, application) == "reject"
    assert decide("hostssl", "arkray", APP, application) == scram
    assert decide("hostnossl", "arkray", APP, application) == "reject"
    assert decide("hostssl", "arkray", "someone_else", application) == "reject"
    # From the migrate job's network: the owner, on the application's database.
    assert decide("hostssl", "arkray", OWNER, migrate) == scram
    assert decide("hostnossl", "arkray", OWNER, migrate) == "reject"
    assert decide("hostssl", "postgres", OWNER, migrate) == "reject"
    assert decide("hostssl", "arkray", SUPERUSER, migrate) == "reject"
    # Anywhere else: nothing.
    assert decide("hostssl", "arkray", APP, "203.0.113.9") == "reject"
    assert decide("hostssl", "arkray", APP, "2001:db8::1") == "reject"
    # The superuser on the local socket only; nobody else there.
    assert decide("local", "postgres", SUPERUSER) == scram
    assert decide("local", "arkray", SUPERUSER) == scram
    assert decide("local", "arkray", OWNER) == "reject"
    assert decide("local", "arkray", APP) == "reject"
    # A restore into the documented database (INFRA-1): the owner from migrate's network,
    # the application role for the release check; any other name stays refused.
    assert decide("hostssl", RESTORE_DATABASE, OWNER, migrate) == scram
    assert decide("hostssl", RESTORE_DATABASE, APP, application) == scram
    assert decide("hostssl", RESTORE_DATABASE, OWNER, application) == "reject"
    assert decide("hostssl", RESTORE_DATABASE, SUPERUSER, migrate) == "reject"
    for other in ("arkray_replaced", "arkray_restored2", "template1"):
        assert decide("hostssl", other, OWNER, migrate) == "reject", other
        assert decide("hostssl", other, APP, application) == "reject", other
    # Physical replication (pg_basebackup, pgBackRest and WAL-G inside the server's own
    # container; INFRA-2): the superuser on the socket only.
    assert decide("local", REPLICATION, SUPERUSER) == scram
    assert decide("local", REPLICATION, OWNER) == "reject"
    assert decide("hostssl", REPLICATION, SUPERUSER, migrate) == "reject"
    assert decide("hostssl", REPLICATION, APP, application) == "reject"


def test_every_rule_is_scram_or_reject_and_everything_else_is_rejected():
    rules = hba_rules()
    methods = {rule[-1] for rule in rules}
    assert methods == {"scram-sha-256", "reject"}
    assert rules[-4:] == [
        ["local", "all", "all", "reject"],
        ["local", REPLICATION, "all", "reject"],
        ["host", "all", "all", "all", "reject"],
        ["host", REPLICATION, "all", "all", "reject"],
    ]
    # The superuser appears on local lines alone; the owner only from its network.
    for rule in rules:
        if SUPERUSER in rule[2].split(","):
            assert rule[0] == "local", rule
        if OWNER in rule[2].split(","):
            assert rule[0] == "hostssl", rule
            assert ipaddress.ip_network(rule[3]) == owner_subnet(), rule


def _volume_targets(service: dict[str, Any]) -> dict[str, str]:
    targets = {}
    for entry in service.get("volumes") or []:
        source, _, rest = str(entry).partition(":")
        targets[rest.split(":")[0]] = source
    return targets


def test_postgres_reads_the_overlays_rules_and_shares_its_socket_with_the_role_setup_alone():
    services = merged()
    postgres = services["postgres"]
    command = " ".join(postgres["command"])
    assert f"hba_file={HBA_FILE}" in command
    assert "password_encryption=scram-sha-256" in command
    assert postgres["environment"]["POSTGRES_USER"] == SUPERUSER
    mounts = [str(entry) for entry in postgres["volumes"]]
    assert f"./infrastructure/postgres/pg_hba.conf:{HBA_FILE}:ro" in mounts
    assert _volume_targets(postgres)[SOCKET_DIR] == SOCKET_VOLUME
    assert SOCKET_DIR not in [str(entry).split(":")[0] for entry in postgres.get("tmpfs") or []]
    with_socket = sorted(
        name
        for name, service in services.items()
        if SOCKET_VOLUME in _volume_targets(service).values()
    )
    assert with_socket == ["db-roles", "postgres"]
    roles = services["db-roles"]
    assert roles.get("network_mode") == "none"  # the socket is its only way in
    assert _volume_targets(roles)[SOCKET_DIR] == SOCKET_VOLUME
    assert roles["command"][:4] == ["psql", "-h", SOCKET_DIR, "-U"]
    assert roles["command"][4] == SUPERUSER


def test_only_the_migrate_job_joins_the_owners_network():
    services = merged()
    network = load(PRODUCTION)["networks"][OWNER_NETWORK]
    assert network["internal"] is True
    assert owner_subnet().num_addresses == 16
    on_owner = sorted(
        name
        for name, service in services.items()
        if OWNER_NETWORK in (service.get("networks") or [])
    )
    assert on_owner == ["migrate", "postgres"]
    # Only there: on `default` too, `postgres` could resolve to its other address and the
    # owner's connection would come from a network the rules refuse it on.
    assert services["migrate"]["networks"] == [OWNER_NETWORK]
    assert sorted(services["postgres"]["networks"]) == ["db-owner", "default"]
    # The owner's password reaches the migrate job and the role setup only.
    owner_password = "${ARKRAY_PROD_DB_OWNER_PASSWORD"
    holders = sorted(
        name
        for name, service in services.items()
        if any(owner_password in value for value in environment(service).values())
    )
    assert holders == [GUARD, "db-roles", "migrate"]


def test_the_runbooks_restore_database_is_the_one_pg_hba_admits():
    """The restore runbook's commands, scripts/restore.sh's example and the production
    template's restore line all name RESTORE_DATABASE, and pg_hba.conf admits exactly
    `arkray` and it for the owner (from `db-owner`) and the application role."""
    admitted = {rule[2]: rule[1] for rule in hba_rules() if rule[0] == "hostssl"}
    assert admitted == {OWNER: f"arkray,{RESTORE_DATABASE}", APP: f"arkray,{RESTORE_DATABASE}"}
    text = _text(RUNBOOKS)
    section = text[
        text.index("## Restore from backup") : text.index("## Recovery validation checklist")
    ]
    assert f"-v database={RESTORE_DATABASE}" in section
    assert f"/{RESTORE_DATABASE}?sslmode=verify-full" in section
    assert f"ALTER DATABASE {RESTORE_DATABASE} RENAME TO arkray" in section
    assert "project=arkray-prod" in section
    assert '--network "${project}_db-owner"' in section
    assert "scripts/prod-compose.sh" in section
    restore_sh = _text(ROOT / "scripts" / "restore.sh")
    assert f"/{RESTORE_DATABASE}?sslmode=verify-full" in restore_sh
    template = _text(ROOT / "infrastructure" / "env" / "production.env.example")
    assert f"/{RESTORE_DATABASE}?sslmode=verify-full" in template
