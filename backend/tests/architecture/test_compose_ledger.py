"""Every container that is told where the erasure ledger is can read it (final audit ARCH-1).

worker-ai, worker-index and beat carried ERASURE_LEDGER_URL=file:///var/lib/arkray/ledger
(the shared backend environment) without the `ledger` volume: inside them the path was the
image's empty directory. After the first erasure-like append anywhere (a deleted attachment
is enough) their gate read the ledger as rolled back ("ahead") and every task they ran was
skipped. So, in the development file alone and merged with the production overlay (as
`docker compose -f docker-compose.yml -f infrastructure/compose.production.yml` merges
them: environments by key, volumes by target path):

- a service whose ERASURE_LEDGER_URL is a file:// URL mounts a named volume at that path;
- every such service mounts the same volume, and no other service mounts it;
- every Celery container has a health check that runs `manage.py ledger_status` (a worker
  with `--ping-worker` and its node name), and beat keeps its schedule file in a writable
  place (ARCH-2: the image's working directory isn't writable by the app user).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from tests.architecture.test_compose_production import BASE, PRODUCTION, environment, load


def _volumes(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The service's mounts by target path (short `source:target[:mode]` or long syntax)."""
    mounts: dict[str, dict[str, Any]] = {}
    for item in service.get("volumes") or []:
        if isinstance(item, str):
            parts = item.split(":")
            # A Windows bind source ("C:\\...") never appears in these files; tmpfs is apart.
            source, target = (parts[0], parts[1]) if len(parts) > 1 else ("", parts[0])
            mount: dict[str, Any] = {"source": source, "target": target}
            mount["type"] = "bind" if source.startswith((".", "/", "~")) else "volume"
            mount["read_only"] = len(parts) > 2 and "ro" in parts[2].split(",")
        else:
            mount = dict(item)
        mounts[str(mount["target"]).rstrip("/")] = mount
    return mounts


def merged_services(*paths: Path) -> dict[str, dict[str, Any]]:
    """The files merged the way Compose merges them, for what these rules read."""
    services: dict[str, dict[str, Any]] = {}
    for path in paths:
        for name, service in (load(path).get("services") or {}).items():
            merged = services.setdefault(
                name, {"environment": {}, "volumes": {}, "healthcheck": None, "command": None}
            )
            merged["environment"].update(environment(service))
            merged["volumes"].update(_volumes(service))
            if service.get("healthcheck"):
                merged["healthcheck"] = {**(merged["healthcheck"] or {}), **service["healthcheck"]}
            if service.get("command") is not None:
                merged["command"] = service["command"]
            for key in ("tmpfs", "read_only"):
                if key in service:
                    merged[key] = service[key]
    return services


def _ledger_path(url: str) -> str | None:
    parts = urlsplit(url)
    return unquote(parts.path).rstrip("/") if parts.scheme == "file" else None


def ledger_problems(*paths: Path) -> list[str]:
    services = merged_services(*paths)
    problems: list[str] = []
    sources: dict[str, str] = {}
    for name, service in sorted(services.items()):
        path = _ledger_path(service["environment"].get("ERASURE_LEDGER_URL", ""))
        if path is None:
            continue
        mount = service["volumes"].get(path)
        if mount is None or mount.get("type", "volume") != "volume" or not mount.get("source"):
            problems.append(
                f"{name}: ERASURE_LEDGER_URL names {path}, but no volume is mounted there"
            )
            continue
        if mount.get("read_only"):
            problems.append(f"{name}: the ledger volume is mounted read-only")
        sources[name] = str(mount["source"])
    if len(set(sources.values())) > 1:
        problems.append(f"the ledger services don't share one volume: {sorted(sources.items())}")
    shared = set(sources.values())
    for name, service in sorted(services.items()):
        if name in sources:
            continue
        for mount in service["volumes"].values():
            if mount.get("type", "volume") == "volume" and mount.get("source") in shared:
                problems.append(f"{name}: mounts the ledger volume without being a ledger service")
    return problems


def _is_celery(service: dict[str, Any]) -> str | None:
    command = service.get("command") or []
    if isinstance(command, str):
        command = command.split()
    if command[:1] != ["celery"]:
        return None
    return "beat" if "beat" in command else "worker"


def _node_name(command: list[str]) -> str:
    """The worker's `--hostname name@%h` name (Celery's default: celery)."""
    for flag in ("--hostname", "-n"):
        if flag in command:
            return command[command.index(flag) + 1].split("@", 1)[0]
    return "celery"


def health_problems(*paths: Path) -> list[str]:
    problems: list[str] = []
    for name, service in sorted(merged_services(*paths).items()):
        kind = _is_celery(service)
        if kind is None:
            continue
        test = (service.get("healthcheck") or {}).get("test") or []
        if "ledger_status" not in test:
            problems.append(f"{name}: no health check running manage.py ledger_status")
        elif kind == "worker":
            expected = ["--ping-worker", _node_name(service["command"])]
            if test[test.index("ledger_status") + 1 :] != expected:
                problems.append(f"{name}: its health check doesn't ping {expected[1]}@<host>")
        if kind == "beat":
            command = service["command"]
            schedule = command[command.index("--schedule") + 1] if "--schedule" in command else ""
            writable = [str(t).split(":")[0] for t in service.get("tmpfs") or []]
            writable += list(service["volumes"])
            if not any(schedule.startswith(place.rstrip("/") + "/") for place in writable):
                problems.append(f"{name}: beat's --schedule isn't in a tmpfs or a volume")
    return problems


def test_every_ledger_service_mounts_the_shared_ledger_volume():
    assert ledger_problems(BASE) == []
    assert ledger_problems(BASE, PRODUCTION) == []


def test_the_scan_sees_every_backend_image_service():
    """The rule must not pass vacuously: the services carrying the URL are the ones the
    backend image runs."""
    services = merged_services(BASE, PRODUCTION)
    carrying = {
        name
        for name, service in services.items()
        if _ledger_path(service["environment"].get("ERASURE_LEDGER_URL", ""))
    }
    assert carrying == {"migrate", "backend", "worker", "worker-index", "worker-ai", "beat"}
    for name in ("nginx", "frontend", "postgres", "redis"):
        assert "/var/lib/arkray/ledger" not in services[name]["volumes"], name


def test_every_celery_container_reports_its_ledger_gate_and_liveness():
    assert health_problems(BASE) == []
    assert health_problems(BASE, PRODUCTION) == []


def test_a_service_without_the_volume_is_reported(tmp_path):
    """The rule itself, on a file shaped like the audited one."""
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "x-env: &env\n"
        "  ERASURE_LEDGER_URL: file:///var/lib/arkray/ledger\n"
        "services:\n"
        "  worker:\n"
        "    environment: *env\n"
        "    command: [celery, -A, config, worker]\n"
        "    volumes: [ledger:/var/lib/arkray/ledger]\n"
        "  worker-ai:\n"
        "    environment: *env\n"
        "    command: [celery, -A, config, worker, --hostname, ai@%h]\n"
        "  nginx:\n"
        "    volumes: [ledger:/srv/ledger:ro]\n",
        encoding="utf-8",
    )
    assert ledger_problems(compose) == [
        "worker-ai: ERASURE_LEDGER_URL names /var/lib/arkray/ledger, but no volume is mounted"
        " there",
        "nginx: mounts the ledger volume without being a ledger service",
    ]
    assert health_problems(compose) == [
        "worker: no health check running manage.py ledger_status",
        "worker-ai: no health check running manage.py ledger_status",
    ]
