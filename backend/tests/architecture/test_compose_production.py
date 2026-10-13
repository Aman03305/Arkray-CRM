"""The production-shaped Compose stack must not inherit a developer's settings
(whole-software audit, P2).

Compose interpolates `${VAR:-default}` from the shell *and from the repository's .env*, for
every project, `-p arkray-prod` included. A developer's `AI_ENABLED=false` there turned Ask
Arkray off on a backend recreated alone, while the workers kept it on: 271 notes went
unindexed without a log line. So every application setting the development file takes from
the environment with a fallback must be pinned by the production overlay (or required there
with `:?`), apart from the few listed here with their reason.
"""

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / "docker-compose.yml"
PRODUCTION = ROOT / "infrastructure" / "compose.production.yml"
WITH_FALLBACK = re.compile(r"\$\{[A-Z_]+:-[^}]*\}")
# Settings the production stack may take from the environment, and why.
ALLOWED = {
    # A secret, optional: the production overlay takes it from its production-only name
    # (ARKRAY_PROD_ANTHROPIC_API_KEY, never the developer's .env; adversarial review of
    # 73ff1e3, M4); empty means "no model" (none mode).
    ("worker-ai", "ANTHROPIC_API_KEY"),
}


class ComposeLoader(yaml.SafeLoader):
    """Compose's own tags (`!reset`, `!override`) read as the plain values they tag."""


def _untagged(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)  # type: ignore[arg-type]


for _tag in ("!reset", "!override"):
    ComposeLoader.add_constructor(_tag, _untagged)


def load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeLoader)  # noqa: S506 — a SafeLoader
    return loaded


def environment(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment") or {}
    return {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}


def test_every_setting_with_a_development_fallback_is_pinned_for_production():
    base, production = load(BASE), load(PRODUCTION)
    unpinned = []
    for name, service in production["services"].items():
        inherited = environment(base["services"].get(name, {}))
        overridden = environment(service)
        for key, value in inherited.items():
            if not WITH_FALLBACK.search(value) or (name, key) in ALLOWED:
                continue
            pinned = overridden.get(key)
            if pinned is None or WITH_FALLBACK.search(pinned):
                unpinned.append((name, key))
    assert not unpinned, sorted(unpinned)


def test_the_scan_sees_the_settings_that_were_inherited():
    """The shared block is merged into each service, so the scan sees it per service."""
    backend = environment(load(BASE)["services"]["backend"])
    assert WITH_FALLBACK.search(backend["AI_ENABLED"])
    production = environment(load(PRODUCTION)["services"]["backend"])
    assert production["AI_ENABLED"] == "true"
    # Required, under its production-only name (M4: test_compose_credentials.py).
    assert production["METRICS_TOKEN"].startswith("${ARKRAY_PROD_METRICS_TOKEN:?")


TMPFS_SIZE = re.compile(r"(?:^|,)size=(\d+)([kmg])(?:,|$)")
UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}
TMP = "/tmp"  # noqa: S108 — a mount point inside the containers, not a file used here


def tmpfs_sizes(service: dict[str, Any]) -> dict[str, int | None]:
    """Each tmpfs mount's path -> its size in bytes (None: no `size=`, i.e. half the host's
    memory, whatever the container's own limit)."""
    sizes: dict[str, int | None] = {}
    for entry in service.get("tmpfs") or []:
        assert isinstance(entry, str), entry  # the short syntax, "path[:options]"
        path, _, options = entry.partition(":")
        found = TMPFS_SIZE.search(options)
        sizes[path] = int(found.group(1)) * UNITS[found.group(2)] if found else None
    return sizes


def memory_limit(service: dict[str, Any]) -> int:
    value = str(service["deploy"]["resources"]["limits"]["memory"])
    return int(value[:-1]) * UNITS[value[-1]]


def production_services() -> dict[str, dict[str, Any]]:
    """Each production service's tmpfs and resource limits as Compose merges the two files:
    the overlay's `tmpfs` replaces the development one (`!override`), otherwise it's inherited."""
    base, production = load(BASE)["services"], load(PRODUCTION)["services"]
    merged = {}
    for name in base.keys() | production.keys():
        service = {**base.get(name, {}), **production.get(name, {})}
        merged[name] = service
    return merged


def test_every_tmpfs_is_capped():
    """Deep security audit H-1 (follow-up): a tmpfs is memory charged to its container, and
    without `size=` it may grow to half the host's memory whatever the container's limit, so
    whatever fills it (uploads spooled by the API, export ZIPs, clamd's unpacked documents)
    ends in an OOM kill instead of a refused write. Every mount in every production service
    has a size, and together they take at most half the container's memory limit."""
    services = production_services()
    uncapped, too_big = [], []
    for name, service in sorted(services.items()):
        sizes = tmpfs_sizes(service)
        uncapped += [(name, path) for path, size in sizes.items() if size is None]
        if sizes:
            total = sum(size or 0 for size in sizes.values())
            if total > memory_limit(service) // 2:
                too_big.append((name, total, memory_limit(service)))
    assert not uncapped, uncapped
    assert not too_big, too_big
    # The overlay's own sizes are what runs: the development `/tmp` is replaced, not doubled.
    assert services["backend"]["tmpfs"] == [f"{TMP}:size=256m"]


def test_every_tmpfs_holds_what_is_written_there():
    """Capped, not starved: each size covers its worst legitimate case."""
    from django.conf import settings

    services = production_services()
    mb = UNITS["m"]

    def size(name: str, path: str) -> int:
        value = tmpfs_sizes(services[name])[path]
        assert value is not None, (name, path)
        return value

    # The API: an upload spooled per gunicorn worker (at most 8, sync; gunicorn.conf.py) up to
    # ATTACHMENT_MAX_BYTES, plus the storage fetches in flight (a download's copy).
    gunicorn = (ROOT / "backend" / "gunicorn.conf.py").read_text(encoding="utf-8")
    assert "multiprocessing.cpu_count() * 2 + 1, 8)" in gunicorn
    fetches = settings.ATTACHMENT_STORAGE_MAX_IN_FLIGHT_SHARED
    assert size("backend", TMP) >= (8 + fetches) * settings.ATTACHMENT_MAX_BYTES
    # The default worker: per Celery process, an export ZIP (EXPORT_MAX_BYTES) built beside a
    # file fetched from storage.
    (concurrency,) = re.findall(
        r'"--queues", "default", "--concurrency", "(\d+)"', BASE.read_text(encoding="utf-8")
    )
    per_task = settings.EXPORT_MAX_BYTES + settings.ATTACHMENT_MAX_BYTES
    assert size("worker", TMP) >= int(concurrency) * per_task
    # clamd: up to MaxThreads scans at once, each up to MaxScanSize unpacked.
    clamd = (ROOT / "infrastructure" / "clamav" / "clamd.conf").read_text(encoding="utf-8")
    (threads,) = re.findall(r"^MaxThreads (\d+)$", clamd, re.M)
    (scan,) = re.findall(r"^MaxScanSize (\d+)M$", clamd, re.M)
    assert size("clamav", TMP) >= int(threads) * int(scan) * mb
    # nginx: see test_edge_limits.py (uploads per client x body size, the response cap).
    assert size("nginx", "/var/cache/nginx") >= 64 * mb


def test_every_edge_tmpfs_is_capped():
    """Deep security audit H-1: nginx writes request bodies (and slowly read responses) to
    /var/cache/nginx before anything is authenticated, and a tmpfs is memory charged to the
    container. Uncapped, 24 slow 10 MB uploads from one address got the edge OOM-killed and
    cut every user off; capped, a full temp area refuses what needs it and nothing grows."""
    nginx = load(PRODUCTION)["services"]["nginx"]
    sizes = tmpfs_sizes(nginx)
    assert "/var/cache/nginx" in sizes
    assert not [path for path, size in sizes.items() if size is None], sizes
    # Together at most half the memory limit: the rest is nginx's own (its workers, TLS and
    # the requests in flight; about 30 MB idle, under 100 MB under the attack, measured).
    assert sum(size or 0 for size in sizes.values()) <= memory_limit(nginx) // 2
