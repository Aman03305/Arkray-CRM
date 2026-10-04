"""The shipped alert rules (infrastructure/alerts/arkray.rules.yml) name only metrics the
application exports, and link runbook sections that exist (whole-software audit: the alerts
were prose only)."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
RULES = ROOT / "infrastructure" / "alerts" / "arkray.rules.yml"
METRIC = re.compile(r"\barkray_[a-z_]+")


def rules() -> list[dict[str, object]]:
    loaded = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    return [rule for group in loaded["groups"] for rule in group["rules"]]


def exported_metrics() -> set[str]:
    source = "".join(
        path.read_text(encoding="utf-8")
        for folder in ("arkray", "config")
        for path in (ROOT / "backend" / folder).rglob("*.py")
        if "tests" not in path.parts
    )
    return set(re.findall(r'"(arkray_[a-z_]+)"', source))


def anchors(markdown: str) -> set[str]:
    return {
        re.sub(r"[^a-z0-9 -]", "", heading.lower()).replace(" ", "-")
        for heading in re.findall(r"^## (.+)$", markdown, flags=re.MULTILINE)
    }


def test_every_alert_uses_exported_metrics():
    exported = exported_metrics()
    assert "arkray_outbox_oldest_undelivered_seconds" in exported  # the scan works
    for rule in rules():
        named = set(METRIC.findall(str(rule["expr"])))
        assert named, rule["alert"]
        assert named <= exported, (rule["alert"], sorted(named - exported))


def test_every_alert_links_a_runbook_section_that_exists():
    sections = anchors((ROOT / "docs" / "runbooks.md").read_text(encoding="utf-8"))
    for rule in rules():
        link = str(rule["annotations"]["runbook"])
        page, _, section = link.partition("#")
        assert page == "docs/runbooks.md", rule["alert"]
        assert section in sections, (rule["alert"], section)
