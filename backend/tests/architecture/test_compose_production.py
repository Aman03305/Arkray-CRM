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
    # A secret: only the operator's shell has it; empty means "no model" (none mode).
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
    assert production["METRICS_TOKEN"].startswith("${METRICS_TOKEN:?")
