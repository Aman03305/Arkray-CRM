"""The production environment template (infrastructure/env/production.env.example) is
complete, holds no secret, and documents a configuration production accepts and nothing
weaker (deployment preparation, 2026-10-12).

- **Complete**: every variable the backend reads (settings, gunicorn, the transport rules), the
  backup and restore scripts, the database provisioning script, ClamAV's health check and
  the web tier read is in it, unless it is listed below as never set by a deployment, with
  the reason. Nothing it sets is read by nobody.
- **No secret**: every secret-bearing value is a `change-me-...` placeholder (or empty), no
  value has a known key format or a high-entropy token, and every placeholder has a filling
  rule here (a new one can't slip in unfilled).
- **Accepted, and nothing weaker**: filled in (placeholders replaced by generated values,
  mounted files created), production settings import cleanly for every backend service and
  variant the template describes, and `manage.py check --deploy` passes with only the HSTS
  policy notes; the optional lines show exactly the values production has without them;
  each security setting weakened one at a time is refused at startup, naming it; and the
  unfilled template is refused too.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shlex
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
REPOSITORY = BACKEND.parent
TEMPLATE = REPOSITORY / "infrastructure" / "env" / "production.env.example"

ACTIVE = re.compile(r"([A-Z][A-Z0-9_]*)=(.*)")
OPTIONAL = re.compile(r"# ([A-Z][A-Z0-9_]*)=(.*)")
OVERLAY = re.compile(r"#\[([a-z][a-z0-9-]*)\] ([A-Z][A-Z0-9_]*)=(.*)")
PLACEHOLDER = re.compile(r"change-me-[a-z0-9]+(?:-[a-z0-9]+)*")
MOUNTED = "/etc/arkray/"

# The backend image's services and variants (their overlays apply on top of the shared lines).
BACKEND_OVERLAYS = (
    "api",
    "migrate",
    "worker-outbox",
    "worker-default",
    "worker-index",
    "worker-ai",
    "single-host",
)
OTHER_OVERLAYS = ("web", "web-build", "provisioning", "backup", "restore", "clamav")

# Read by the code, never set by a deployment that serves real users. The template names
# each one in a comment (so an operator learns it exists and why not), and assigns none.
NEVER_SET = {
    "DJANGO_ALLOW_INSECURE_LOCAL_HTTP": "the local stack's plain-HTTP opt-in",
    "LOG_EXCEPTION_MESSAGES": "local development only; production refuses it",
    "ANTHROPIC_LOG": "production refuses it (it logs whole requests)",
    "ANTHROPIC_BASE_URL": "production refuses it (AI_LLM_BASE_URL decides)",
    "ANTHROPIC_CUSTOM_HEADERS": "production refuses it",
    "ATTACHMENT_SCANNER_WAIVED": "the organisation's written acceptance of R94, never a default",
    "BACKUP_ALLOW_UNENCRYPTED": "throwaway drills only",
    "RESTORE_UNVERIFIED": "throwaway drills only",
}
# Read by the code, but not configuration: the platform's, a tool's own, or derived.
NOT_CONFIGURATION = {
    "PGHOST": "libpq's fallback; DATABASE_URL names the server",
    "PGHOSTADDR": "libpq's fallback; DATABASE_URL names the server",
    "PGSERVICE": "libpq's service files: the backup scripts refuse it",
    "PGSSLMODE": "libpq's; the backup scripts default it to verify-full",
    "PGOPTIONS": "libpq's; restore.sh sets its own index-build settings",
    "BASH_REMATCH": "bash's",
    "TMPDIR": "the system's temporary directory (restore.sh: RESTORE_WORK_DIR first)",
}
# Set by the template, read by something this test doesn't scan.
READ_ELSEWHERE = {
    "ADMIN_DATABASE_URL": "roles.sql's usage line (psql connects with it)",
    "FRESHCLAM_CHECKS": "the clamav/clamav image's entrypoint",
    "HOSTNAME": "the Next.js standalone server",
}

# A generated value for every placeholder: the template may hold no other.
FILL: dict[str, Callable[[], str]] = {
    "change-me-django-secret-key": lambda: secrets.token_hex(32),
    "change-me-edge-proxy-address": lambda: "10.0.0.10",
    "change-me-app-password": lambda: secrets.token_hex(16),
    "change-me-owner-password": lambda: secrets.token_hex(16),
    "change-me-broker-password": lambda: secrets.token_hex(16),
    "change-me-cache-password": lambda: secrets.token_hex(16),
    "change-me-smtp-user": lambda: "arkray-mailer",
    "change-me-smtp-password": lambda: secrets.token_hex(16),
    "change-me-attachments-bucket": lambda: "arkray-attachments-test",
    "change-me-erasure-ledger-bucket": lambda: "arkray-erasure-ledger-test",
    "change-me-erasure-ledger-key": lambda: secrets.token_hex(24),
    "change-me-metrics-token": lambda: secrets.token_hex(24),
    "change-me-anthropic-api-key": lambda: secrets.token_hex(24),
    "change-me-db-admin-user": lambda: "dbadmin",
    "change-me-db-admin-password": lambda: secrets.token_hex(16),
    "change-me-backup-user": lambda: "arkray_backup",
    "change-me-backup-password": lambda: secrets.token_hex(16),
}


@dataclass
class Template:
    shared: dict[str, str] = field(default_factory=dict)
    optional: dict[str, str] = field(default_factory=dict)
    overlays: dict[str, dict[str, str]] = field(default_factory=dict)
    comments: str = ""

    def assigned(self) -> set[str]:
        names = set(self.shared) | set(self.optional)
        for overlay in self.overlays.values():
            names |= set(overlay)
        return names

    def values(self) -> list[tuple[str, str]]:
        found = [*self.shared.items(), *self.optional.items()]
        for overlay in self.overlays.values():
            found += overlay.items()
        return found


def parse(text: str) -> Template:
    """Strictly: every line is blank, a comment, or one of the three forms; no duplicates."""
    template = Template()
    comments: list[str] = []

    def put(target: dict[str, str], name: str, value: str, line: str) -> None:
        assert name not in target, f"{name} twice: {line!r}"
        # No value has a space: a line that has one is a comment that happens to start like
        # an assignment (reword it), or a value a shell would split.
        assert not re.search(r"\s", value), f"a space in {line!r}"
        target[name] = value

    for number, line in enumerate(text.split("\n"), start=1):
        assert line == line.rstrip(), f"line {number}: trailing whitespace"
        if overlay := OVERLAY.fullmatch(line):
            name = overlay[1]
            put(template.overlays.setdefault(name, {}), overlay[2], overlay[3], line)
        elif optional := OPTIONAL.fullmatch(line):
            put(template.optional, optional[1], optional[2], line)
        elif active := ACTIVE.fullmatch(line):
            put(template.shared, active[1], active[2], line)
        else:
            assert line == "" or line.startswith("#"), f"line {number}: {line!r}"
            comments.append(line)
    template.comments = "\n".join(comments)
    return template


TEXT = TEMPLATE.read_text(encoding="utf-8")
PARSED = parse(TEXT)


# --- what the code reads ---------------------------------------------------------------------
PYTHON_READ = re.compile(
    r"""(?:\benv(?:\.\w+)?\(|os\.environ(?:\.get|\.setdefault)?\(|os\.environ\[)\s*["']"""
    r"""([A-Z][A-Z0-9_]+)["']"""
    r"""|["']([A-Z][A-Z0-9_]+)["']\s+(?:not\s+)?in\s+os\.environ""",
    re.S,
)
SHELL_READ = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)")
PSQL_GETENV = re.compile(r"\\getenv\s+\w+\s+([A-Z][A-Z0-9_]*)")
NODE_READ = re.compile(r"process\.env\.([A-Z][A-Z0-9_]*)")


def _python_sources() -> list[Path]:
    application = [
        path
        for path in sorted((BACKEND / "arkray").rglob("*.py"))
        if "tests" not in path.parts and "migrations" not in path.parts
    ]
    return [*sorted((BACKEND / "config").rglob("*.py")), BACKEND / "gunicorn.conf.py", *application]


def variables_read() -> dict[str, set[str]]:
    """Each variable read, and where."""
    found: dict[str, set[str]] = {}

    def add(name: str, path: Path) -> None:
        found.setdefault(name, set()).add(path.relative_to(REPOSITORY).as_posix())

    for path in _python_sources():
        for match in PYTHON_READ.finditer(path.read_text(encoding="utf-8")):
            add(match[1] or match[2], path)
    shell = [
        REPOSITORY / "scripts" / "backup.sh",
        REPOSITORY / "scripts" / "restore.sh",
        REPOSITORY / "infrastructure" / "clamav" / "healthcheck.sh",
    ]
    for path in shell:
        code = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").split("\n")
            if not line.lstrip().startswith("#")
        )
        for name in SHELL_READ.findall(code):
            add(name, path)
    roles = REPOSITORY / "infrastructure" / "postgres" / "roles.sql"
    for name in PSQL_GETENV.findall(roles.read_text(encoding="utf-8")):
        add(name, roles)
    frontend = REPOSITORY / "frontend"
    web = [frontend / "next.config.ts"] + [
        path for path in sorted((frontend / "src").rglob("*.ts*")) if ".test." not in path.name
    ]
    for path in web:
        for name in NODE_READ.findall(path.read_text(encoding="utf-8")):
            add(name, path)
    return found


def test_the_scan_finds_what_it_should():
    found = variables_read()
    # Settings (multi-line and membership reads), gunicorn, the transport rules' libpq
    # fallbacks, both scripts, roles.sql, the ClamAV health check and the web tier.
    for name in (
        "AI_EMBEDDING_MODEL_DIR",
        "EMAIL_URL",
        "WEB_CONCURRENCY",
        "PGHOSTADDR",
        "BACKUP_GPG_SIGNING_KEY_FILE",
        "RESTORE_WORK_DIR",
        "OWNER_PASSWORD",
        "CLAMAV_MAX_SIGNATURE_AGE_S",
        "API_ORIGIN",
    ):
        assert name in found, name
    assert len(found) > 120


def test_the_template_parses():
    assert TEXT.endswith("\n")
    assert "\r" not in TEXT
    assert set(PARSED.overlays) == {*BACKEND_OVERLAYS, *OTHER_OVERLAYS}
    for name, value in PARSED.values():
        # docker --env-file and Compose keep quotes and "# ..." after a value literally.
        assert not value.startswith(("'", '"')), name
        assert " #" not in value, name
        assert value == value.strip(), name


def test_every_variable_read_is_in_the_template():
    read = set(variables_read())
    missing = sorted(read - PARSED.assigned() - set(NEVER_SET) - set(NOT_CONFIGURATION))
    assert missing == [], f"add these to {TEMPLATE.name} (or to an allowlist, with why)"


def test_the_template_sets_nothing_nobody_reads():
    stale = sorted(PARSED.assigned() - set(variables_read()) - set(READ_ELSEWHERE))
    assert stale == []


def test_variables_never_set_are_named_and_not_assigned():
    for name in NEVER_SET:
        assert name not in PARSED.assigned(), name
        assert re.search(rf"\b{name}\b", PARSED.comments), f"name {name} in a comment"
    for name in NOT_CONFIGURATION:
        assert name not in PARSED.assigned(), name


# --- no secrets --------------------------------------------------------------------------------
SECRET_NAME = re.compile(r"(?:^|_)(?:SECRET|PASSWORD|TOKEN|KEYS?|KEY_ID|FALLBACKS)$")
URL_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^:/@]*:([^@]*)@")
KNOWN_FORMATS = re.compile(
    r"sk-ant-|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN|xox[abposr]-"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|SG\.[A-Za-z0-9_-]{16,}"
)
# Path and URL segments one at a time: a key that contains "/" still has long runs without it.
TOKEN = re.compile(r"[A-Za-z0-9+=_-]{20,}")


def _entropy(text: str) -> float:
    counts = Counter(text)
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def secret_findings(template: Template) -> list[str]:
    findings: list[str] = []
    for name, value in template.values():
        if SECRET_NAME.search(name) and value and not PLACEHOLDER.fullmatch(value):
            findings.append(f"{name}: a secret-bearing value that isn't a placeholder")
        password = URL_PASSWORD.match(value)
        if password and not PLACEHOLDER.fullmatch(password[1]):
            findings.append(f"{name}: a URL password that isn't a placeholder")
        if KNOWN_FORMATS.search(value):
            findings.append(f"{name}: a known key format")
        for token in TOKEN.findall(PLACEHOLDER.sub("", value)):
            # Words joined by hyphens (paths, names) aren't keys; a key mixes in digits or
            # capitals and is close to random.
            if re.search(r"[0-9A-Z]", token) and _entropy(token) >= 3.5:
                findings.append(f"{name}: {token[:4]}... looks random")
    return findings


def test_no_value_looks_like_a_real_secret():
    assert secret_findings(PARSED) == []


@pytest.mark.parametrize(
    "line",
    [
        "DJANGO_SECRET_KEY=" + "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1eF3hJ5kL7nP9rS1uV3xY5",
        "CELERY_BROKER_URL=rediss://:Zq81Lr0xVt2Pw4Kn@broker.example.com:6380/0",
        "ANTHROPIC_API_KEY=sk-ant-" + "api03-" + "x" * 20,
        "ATTACHMENT_S3_ACCESS_KEY_ID=AKIA" + "ABCDEFGHIJKLMNOP",
        "AI_CHAT_MODEL=Zq81Lr0xVt2Pw4Kn7Hc5Ym3Fj9Bd6Gs",
    ],
)
def test_the_secret_heuristics_catch_real_looking_values(line):
    """The scan above is only as good as these: each real-looking value is caught."""
    assert secret_findings(parse(line + "\n"))


def test_every_placeholder_has_a_filling_rule():
    assert set(PLACEHOLDER.findall(TEXT)) == set(FILL)


# --- accepted, and nothing weaker --------------------------------------------------------------
def image_environment() -> dict[str, str]:
    """The backend image's own ENV (its runtime stage): what a variable left out falls back
    to inside the container."""
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    runtime = dockerfile.rsplit("\nFROM ", 1)[1].replace("\\\n", " ")
    found: dict[str, str] = {}
    for line in runtime.split("\n"):
        if line.startswith("ENV "):
            for pair in shlex.split(line[4:]):
                name, _, value = pair.partition("=")
                if name in PARSED.assigned():
                    found[name] = value
    return found


def filled(mounted: Path, *overlays: str) -> dict[str, str]:
    """The shared lines, then the overlays', placeholders replaced, files created."""
    values = {placeholder: make() for placeholder, make in FILL.items()}
    lines = dict(PARSED.shared)
    for overlay in overlays:
        lines.update(PARSED.overlays[overlay])
    environment: dict[str, str] = {}
    for name, value in lines.items():
        value = PLACEHOLDER.sub(lambda found: values[found[0]], value)
        environment[name] = value.replace(MOUNTED, mounted.as_posix() + "/")
    for path in re.findall(rf"{re.escape(mounted.as_posix())}/[\w./-]+", str(environment)):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).touch()
    return environment


def run(environment: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """A fresh interpreter with exactly this configuration on top of the image's own ENV:
    nothing from the developer's environment that the template or the code could read."""
    drop = set(variables_read()) | PARSED.assigned() | set(NEVER_SET)
    base = {
        key: value
        for key, value in os.environ.items()
        if key not in drop
        and not key.startswith(("DJANGO_", "DB_", "AI_", "ANTHROPIC_", "AWS_", "PG"))
    }
    return subprocess.run(  # noqa: S603 — fixed interpreter and arguments
        [sys.executable, *args],
        cwd=BACKEND,
        env={**base, **image_environment(), **environment},
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


IMPORT = ["-c", "from django.conf import settings; print(settings.DEBUG)"]
DUMP = [
    "-c",
    "import json, config.settings.production as s; print(json.dumps({k: v for k, v in"
    " vars(s).items() if k.isupper()}, default=lambda o: type(o).__name__, sort_keys=True))",
]


def test_the_unfilled_template_is_refused(tmp_path):
    environment = {
        name: value.replace(MOUNTED, tmp_path.as_posix() + "/")
        for name, value in PARSED.shared.items()
    }
    result = run(environment, *IMPORT)
    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY" in result.stderr


@pytest.mark.parametrize("overlay", [None, *BACKEND_OVERLAYS])
def test_the_filled_template_starts(tmp_path, overlay):
    result = run(filled(tmp_path, *([overlay] if overlay else [])), *IMPORT)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.split() == ["False"]


def test_the_filled_template_passes_the_deploy_check(tmp_path):
    """`check --deploy` as CI's image smoke test runs it (no database): only Django's HSTS
    notes, policy choices docs/deployment.md#release-process explains."""
    result = run(filled(tmp_path, "api"), "manage.py", "check", "--deploy")
    assert result.returncode == 0, result.stderr[-2000:]
    reported = set(re.findall(r"\(([a-z_]+\.[A-Z]\d{3})\)", result.stdout + result.stderr))
    assert reported <= {"security.W005", "security.W021"}, result.stdout + result.stderr


def test_the_optional_lines_show_what_production_has_without_them(tmp_path):
    environment = filled(tmp_path)
    without = run(environment, *DUMP)
    with_them = run({**environment, **PARSED.optional}, *DUMP)
    assert without.returncode == 0, without.stderr[-2000:]
    assert with_them.returncode == 0, with_them.stderr[-2000:]
    before, after = json.loads(without.stdout), json.loads(with_them.stdout)
    assert {k: (before.get(k), v) for k, v in after.items() if before.get(k) != v} == {}


def _replace(name: str, old: str, new: str) -> Callable[[dict[str, str]], None]:
    def weaken(environment: dict[str, str]) -> None:
        assert old in environment[name], f"the template's {name} has no {old!r} to weaken"
        environment[name] = environment[name].replace(old, new)

    return weaken


def _set(name: str, value: str) -> Callable[[dict[str, str]], None]:
    def weaken(environment: dict[str, str]) -> None:
        assert environment.get(name) != value, f"the template already has {name}={value}"
        environment[name] = value

    return weaken


def _unset(name: str) -> Callable[[dict[str, str]], None]:
    def weaken(environment: dict[str, str]) -> None:
        assert name in environment, f"the template has no {name} to remove"
        del environment[name]

    return weaken


def _same_as(name: str, other: str) -> Callable[[dict[str, str]], None]:
    def weaken(environment: dict[str, str]) -> None:
        environment[name] = environment[other]

    return weaken


# Each security setting of the template, weakened alone: (how, what the refusal names).
WEAKENED: dict[str, tuple[Callable[[dict[str, str]], None], str]] = {
    "database sslmode=require": (
        _replace("DATABASE_URL", "sslmode=verify-full", "sslmode=require"),
        "DATABASE_URL",
    ),
    "database without its CA": (
        _replace("DATABASE_URL", "&sslrootcert=", "&ignored="),
        "DATABASE_URL",
    ),
    "broker in plain text": (
        _replace("CELERY_BROKER_URL", "rediss://", "redis://"),
        "CELERY_BROKER_URL",
    ),
    "cache in plain text": (
        _replace("REDIS_CACHE_URL", "rediss://", "redis://"),
        "REDIS_CACHE_URL",
    ),
    "broker certificate unchecked": (
        _replace("CELERY_BROKER_URL", "ssl_cert_reqs=required", "ssl_cert_reqs=none"),
        "CELERY_BROKER_URL",
    ),
    "cache without a password": (
        _replace("REDIS_CACHE_URL", "rediss://:", "rediss://@"),
        "REDIS_CACHE_URL",
    ),
    "email in plain text": (_replace("EMAIL_URL", "smtp+tls://", "smtp://"), "EMAIL_URL"),
    "email to a console": (_set("EMAIL_URL", "consolemail://"), "EMAIL_URL"),
    "sender on a reserved domain": (
        _set("DEFAULT_FROM_EMAIL", "no-reply@crm.example"),
        "DEFAULT_FROM_EMAIL",
    ),
    "attachments over http": (
        _set("ATTACHMENT_S3_ENDPOINT_URL", "http://s3.example.com"),
        "ATTACHMENT_S3_ENDPOINT_URL",
    ),
    "ledger over http": (
        _set("ERASURE_LEDGER_S3_ENDPOINT_URL", "http://s3.example.com"),
        "ERASURE_LEDGER_S3_ENDPOINT_URL must be https://",
    ),
    "no scanner, no waiver": (_unset("ATTACHMENT_SCANNER"), "ATTACHMENT_SCANNER"),
    "scanner on a public host": (
        _set("ATTACHMENT_SCANNER", "clamd://clamav.example.com:3310"),
        "ATTACHMENT_SCANNER",
    ),
    "a routable private host": (
        _set("BACKEND_TLS_PRIVATE_HOSTS", "clamav,db.example.com"),
        "BACKEND_TLS_PRIVATE_HOSTS",
    ),
    "no second factor for administrators": (
        _set("MFA_REQUIRED_FOR_ADMINS", "false"),
        "MFA_REQUIRED_FOR_ADMINS",
    ),
    "a privileged database role allowed": (
        _set("DB_REQUIRE_RESTRICTED_ROLE", "false"),
        "DB_REQUIRE_RESTRICTED_ROLE",
    ),
    "HSTS for an hour": (_set("DJANGO_HSTS_SECONDS", "3600"), "DJANGO_HSTS_SECONDS"),
    "no HSTS": (_set("DJANGO_HSTS_SECONDS", "0"), "DJANGO_HSTS_SECONDS"),
    "cookies without Secure": (_set("DJANGO_SECURE_COOKIES", "false"), "DJANGO_SECURE_COOKIES"),
    "no HTTPS redirect": (
        _set("DJANGO_SECURE_SSL_REDIRECT", "false"),
        "DJANGO_SECURE_SSL_REDIRECT",
    ),
    "account links over http": (_replace("APP_BASE_URL", "https://", "http://"), "APP_BASE_URL"),
    "an http origin trusted": (
        _replace("DJANGO_CSRF_TRUSTED_ORIGINS", "https://", "http://"),
        "DJANGO_CSRF_TRUSTED_ORIGINS",
    ),
    "a wildcard host": (_set("DJANGO_ALLOWED_HOSTS", "*"), "DJANGO_ALLOWED_HOSTS"),
    "the local opt-in": (
        _set("DJANGO_ALLOW_INSECURE_LOCAL_HTTP", "true"),
        "DJANGO_ALLOW_INSECURE_LOCAL_HTTP",
    ),
    "proxy count without addresses": (_unset("TRUSTED_PROXY_ADDRESSES"), "TRUSTED_PROXY_ADDRESSES"),
    "model calls over http": (
        _replace("AI_LLM_BASE_URL", "https://", "http://"),
        "AI_LLM_BASE_URL",
    ),
    "the provider key outside worker-ai": (
        _set("ANTHROPIC_API_KEY", "k" * 40),
        "ANTHROPIC_API_KEY",
    ),
    "the SDK's request logging": (_set("ANTHROPIC_LOG", "debug"), "ANTHROPIC_LOG"),
    "a short secret key": (_set("DJANGO_SECRET_KEY", "short-django-key"), "DJANGO_SECRET_KEY"),
    "no erasure ledger": (_unset("ERASURE_LEDGER_URL"), "ERASURE_LEDGER_URL"),
    "a short ledger key": (_set("ERASURE_LEDGER_KEY", "short-ledger-key"), "ERASURE_LEDGER_KEY"),
    "the ledger keyed with the Django key": (
        _same_as("ERASURE_LEDGER_KEY", "DJANGO_SECRET_KEY"),
        "ERASURE_LEDGER_KEY",
    ),
    "a short metrics token": (_set("METRICS_TOKEN", "short-metrics-token"), "METRICS_TOKEN"),
    "exception messages in logs": (
        _set("LOG_EXCEPTION_MESSAGES", "true"),
        "LOG_EXCEPTION_MESSAGES",
    ),
}


@pytest.mark.parametrize("case", sorted(WEAKENED))
def test_each_security_setting_weakened_is_refused(tmp_path, case):
    weaken, named = WEAKENED[case]
    environment = filled(tmp_path, "api")
    weaken(environment)
    result = run(environment, *IMPORT)
    assert result.returncode != 0, f"production accepted: {case}"
    assert "ImproperlyConfigured" in result.stderr
    assert named in result.stderr
