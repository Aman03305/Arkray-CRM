"""The reference stacks' supply chain and least privilege (R128), and the attachment scanner
in the production-shaped one (R94).

- every third-party image (Compose files, the CI service containers, the Dockerfiles' base
  images) is pinned by digest: a moved tag can't change what runs;
- in the production-shaped stack every long-running service has a health check and memory,
  CPU and process limits, and every service runs with a read-only root filesystem, no
  privilege escalation and no capabilities beyond a short, named list;
- the outbox relay and email have a worker of their own, and the default worker's grace
  period outlasts the nightly attachment check;
- ClamAV is reachable only by the scanning worker, on an internal network, and the
  production settings name it (no waiver).

Started for real too (docs/operations.md#production-shaped-stack): every service healthy,
`PING` to clamd from the worker, `manage.py check --deploy`.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

import yaml

from tests.architecture.test_compose_production import BASE, PRODUCTION, ROOT, load

DIGEST = re.compile(r"^[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}$")
CI = ROOT / ".github" / "workflows" / "ci.yml"
DOCKERFILES = [ROOT / "backend" / "Dockerfile", ROOT / "frontend" / "Dockerfile"]
ONE_SHOT = {"credential-guard", "backend-tls", "db-roles", "migrate", "certs"}
# Capabilities a service may add back, and why (everything else drops ALL).
ALLOWED_CAPABILITIES = {
    # nginx's master switches to its worker user and owns its temporary directories.
    "nginx": {"CHOWN", "SETGID", "SETUID"},
    # Hands each server its own TLS key.
    "backend-tls": {"CHOWN"},
}
# Reconcile check: RECONCILE_MAX_SECONDS (15 min) + 120 s hard limit.
RECONCILE_HARD_LIMIT_S = 15 * 60 + 120


def merged() -> dict[str, dict[str, Any]]:
    """Each service's keys as Compose merges the two files (later file wins per key; the
    nested `deploy` and `healthcheck` mappings merge key by key)."""
    services: dict[str, dict[str, Any]] = {}
    for source in (load(BASE)["services"], load(PRODUCTION)["services"]):
        for name, service in source.items():
            target = services.setdefault(name, {})
            for key, value in service.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    target[key] = {**target[key], **value}
                else:
                    target[key] = value
    return services


def _seconds(value: str) -> int:
    total = 0
    for amount, unit in re.findall(r"(\d+)([hms])", value):
        total += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def test_every_third_party_image_is_pinned_by_digest():
    images = []
    for path in (BASE, PRODUCTION):
        images += [s["image"] for s in load(path)["services"].values() if "image" in s]
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        images += [s["image"] for s in (job.get("services") or {}).values()]
    for dockerfile in DOCKERFILES:
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            # `FROM <image> [AS name]`; `FROM <earlier stage>` names no image.
            if parts[:1] == ["FROM"] and ":" in parts[1]:
                images.append(parts[1])
            # The BuildKit frontend is an image too, pulled and run by every build (CI review
            # C14, deployment preparation): `# syntax=<image>`.
            if line.startswith("# syntax="):
                images.append(line.removeprefix("# syntax="))
    assert len(images) >= 10
    unpinned = sorted({image for image in images if not DIGEST.fullmatch(image)})
    assert unpinned == []


def test_every_production_service_is_least_privileged():
    problems = []
    for name, service in sorted(merged().items()):
        if service.get("read_only") is not True:
            problems.append(f"{name}: root filesystem not read-only")
        if service.get("cap_drop") != ["ALL"]:
            problems.append(f"{name}: doesn't drop every capability")
        extra = set(service.get("cap_add") or [])
        if not extra <= ALLOWED_CAPABILITIES.get(name, set()):
            problems.append(f"{name}: adds {sorted(extra)}")
        if "no-new-privileges:true" not in (service.get("security_opt") or []):
            problems.append(f"{name}: may gain privileges")
        limits = ((service.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        if not {"memory", "cpus", "pids"} <= set(limits):
            problems.append(f"{name}: limits {sorted(limits)}")
        if name not in ONE_SHOT and not (service.get("healthcheck") or {}).get("test"):
            # mailpit's and frontend's come from the image or the base file.
            problems.append(f"{name}: no health check")
    assert problems == []


def test_the_data_services_run_as_their_own_users():
    services = merged()
    assert services["postgres"]["user"] == "999:999"
    assert services["redis"]["user"] == services["redis-cache"]["user"] == "999:1000"
    assert services["mailpit"]["user"] == "65534:65534"
    assert services["db-roles"]["user"] == "999:999"
    assert services["clamav"]["user"] == "100:101"  # no capability at all


def test_the_outbox_relay_and_email_have_their_own_worker():
    services = merged()

    def queues(name: str) -> set[str]:
        command = services[name]["command"]
        return set(command[command.index("--queues") + 1].split(","))

    assert queues("worker-outbox") == {"outbox", "email"}
    assert queues("worker") == {"default"}  # attachment scans and the nightly checks
    workers = [n for n, s in services.items() if "--queues" in (s.get("command") or [])]
    every = set().union(*(queues(n) for n in workers))
    assert every == {"outbox", "email", "default", "ai", "ai_index"}
    assert _seconds(services["worker"]["stop_grace_period"]) > RECONCILE_HARD_LIMIT_S


def test_the_reconcile_check_still_fits_the_grace_period():
    from arkray.activities import tasks

    assert tasks.RECONCILE_MAX_SECONDS + 120 == RECONCILE_HARD_LIMIT_S
    limit = tasks.reconcile_check.time_limit
    assert limit is not None
    assert limit <= RECONCILE_HARD_LIMIT_S


def test_clamav_is_reachable_by_the_scanning_worker_only():
    services = merged()
    production = load(PRODUCTION)
    assert production["networks"]["scanner"]["internal"] is True
    on_scanner = sorted(n for n, s in services.items() if "scanner" in (s.get("networks") or []))
    assert on_scanner == ["clamav", "worker"]
    on_egress = sorted(
        n for n, s in services.items() if "clamav-egress" in (s.get("networks") or [])
    )
    assert on_egress == ["clamav"]
    assert "ports" not in services["clamav"]
    assert services["clamav"]["deploy"]["resources"]["limits"]["memory"] in {"2g", "3g"}


def test_production_scans_attachments_with_no_waiver():
    services = merged()
    env = services["backend"]["environment"]
    assert env["ATTACHMENT_SCANNER"] == "clamd://clamav:3310"
    assert env["BACKEND_TLS_PRIVATE_HOSTS"] == "clamav"
    assert "ATTACHMENT_SCANNER_WAIVED" not in env
    text = PRODUCTION.read_text(encoding="utf-8")
    assert "ATTACHMENT_SCANNER_WAIVED" not in text


def test_clamd_accepts_the_largest_attachment():
    from django.conf import settings

    conf = (ROOT / "infrastructure" / "clamav" / "clamd.conf").read_text(encoding="utf-8")
    (value,) = re.findall(r"^StreamMaxLength (\d+)M$", conf, re.M)
    (file_size,) = re.findall(r"^MaxFileSize (\d+)M$", conf, re.M)
    (scan_size,) = re.findall(r"^MaxScanSize (\d+)M$", conf, re.M)
    # ATTACHMENT_MAX_BYTES <= StreamMaxLength <= MaxFileSize <= MaxScanSize: else a file (or
    # a document's unpacked member: a 10 MB xlsx's XML can be several times larger) is
    # refused or over a limit, and stays pending forever (adversarial review P3).
    assert int(value) * 1024 * 1024 >= settings.ATTACHMENT_MAX_BYTES
    assert int(value) <= int(file_size) <= int(scan_size)
    assert int(file_size) >= 8 * int(value)
    # Partly scanned content is an alert (no verdict for the app), never a quiet OK.
    assert re.search(r"^AlertExceedsMax yes$", conf, re.M)
    nginx = (ROOT / "infrastructure" / "nginx" / "arkray.conf").read_text(encoding="utf-8")
    (edge,) = re.findall(r"client_max_body_size (\d+)m;\n\s+error_page 413", nginx)
    assert int(value) >= int(edge)  # nothing the edge lets through is too long for clamd


def test_no_package_is_installed_at_run_time():
    """backend-tls and certs used `apk add openssl` (an unpinned download on every fresh
    volume): the PostgreSQL image's own openssl now."""
    for name, service in merged().items():
        command = service.get("command") or []
        text = " ".join(command) if isinstance(command, list) else str(command)
        for installer in ("apk add", "apt-get", "pip install", "npm install"):
            assert installer not in text, (name, installer)


def _queues(command: list[str]) -> set[str]:
    return (
        set(command[command.index("--queues") + 1].split(",")) if "--queues" in command else set()
    )


def test_every_queue_work_is_sent_to_has_a_consumer():
    """Celery routes, outbox handlers and the default queue, against the workers' --queues
    in both stacks: a queue nobody consumes is work that silently never runs."""
    from django.conf import settings

    from arkray.core import outbox

    routed = {route["queue"] for route in settings.CELERY_TASK_ROUTES.values()}
    routed |= {handler.queue for handler in outbox._HANDLERS.values()}
    routed |= set(settings.OUTBOX_MAX_IN_FLIGHT) | {settings.CELERY_TASK_DEFAULT_QUEUE}
    for services in (load(BASE)["services"], merged()):
        consumed = set().union(*(_queues(s.get("command") or []) for s in services.values()))
        assert routed <= consumed, routed - consumed


def test_the_ledger_reconcile_runs_beside_the_relay():
    """It reopens the API after a failed erasure commit: never behind the default queue's
    long work (the 17-minute nightly check, and its 18-minute drain during a deploy)."""
    from django.conf import settings

    assert settings.CELERY_TASK_ROUTES["privacy.reconcile_ledger"]["queue"] == "outbox"
    assert "outbox" in _queues(merged()["worker-outbox"]["command"])


def test_only_the_edge_proxy_is_believed_and_only_it_reaches_the_api():
    """Adversarial review P2-2: X-Forwarded-For is read only from the address nginx has on
    `api`, an internal network it shares with the API alone; nginx isn't on the network
    the API shares with everything else, so it always reaches the API from that address."""
    services = merged()
    production = load(PRODUCTION)
    env = services["backend"]["environment"]
    nginx_networks = services["nginx"]["networks"]
    address = nginx_networks["api"]["ipv4_address"]
    assert env["TRUSTED_PROXY_ADDRESSES"] == address
    assert env["FORWARDED_ALLOW_IPS"] == address
    assert env["TRUSTED_PROXY_COUNT"] == "1"
    api = production["networks"]["api"]
    assert api["internal"] is True
    subnet = api["ipam"]["config"][0]["subnet"]
    assert ipaddress.ip_address(address) in ipaddress.ip_network(subnet)
    on_api = sorted(n for n, s in services.items() if "api" in (s.get("networks") or []))
    assert on_api == ["backend", "nginx"]
    assert "default" not in nginx_networks
    assert services["frontend"]["networks"] == ["web"]  # pages only; /api goes to Django


def test_clamav_health_includes_signature_freshness():
    """A quietly failing freshclam would leave clamd healthy on ever older signatures."""
    clamav = merged()["clamav"]
    assert clamav["healthcheck"]["test"] == ["CMD", "sh", "/usr/local/bin/arkray-clamav-health"]
    mounts = [v for v in clamav["volumes"] if isinstance(v, str)]
    assert any(
        m.endswith("clamav/healthcheck.sh:/usr/local/bin/arkray-clamav-health:ro") for m in mounts
    )
    script = (ROOT / "infrastructure" / "clamav" / "healthcheck.sh").read_text(encoding="utf-8")
    assert "VERSION" in script
    assert "CLAMAV_MAX_SIGNATURE_AGE_S:-259200" in script
