"""No CRM text leaves the deployment unless someone decides it should (R68, R69).

Ask Arkray runs without a language model (`AI_LLM_PROVIDER=none`) unless a deployment
says otherwise: the settings default, the development compose file's default, the
production compose file and the environment template all say `none`. The production
settings' own constraints on the `anthropic` provider (an https base URL, the key on the ai
worker only, `ANTHROPIC_LOG` and the SDK's variables refused) are tested in
test_production_settings.py; the real-model evaluation's interlocks in
arkray/ai/tests/test_evaluation.py.
"""

from __future__ import annotations

import re

from tests.architecture.test_compose_production import BASE, PRODUCTION, ROOT, load
from tests.architecture.test_production_settings import load_production_settings


def test_the_settings_default_is_no_model():
    base = (ROOT / "backend" / "config" / "settings" / "base.py").read_text(encoding="utf-8")
    assert re.search(r'^AI_LLM_PROVIDER = env\("AI_LLM_PROVIDER", default="none"\)$', base, re.M)
    printed = load_production_settings("settings.AI_LLM_PROVIDER", AI_LLM_PROVIDER=None)
    assert printed.returncode == 0, printed.stderr
    assert printed.stdout.strip() == "none"


def test_every_deployment_template_defaults_to_no_model():
    development, production = load(BASE), load(PRODUCTION)
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    found = {
        "docker-compose.yml": _provider(development),
        "compose.production.yml": _provider(production),
    }
    assert found == {
        "docker-compose.yml": {"${AI_LLM_PROVIDER:-none}"},
        "compose.production.yml": {"none"},
    }
    assert re.findall(r"^AI_LLM_PROVIDER=(.*)$", template, re.M) == ["none"]


def _provider(document: object) -> set[str]:
    """Every value given to AI_LLM_PROVIDER anywhere in a compose document."""
    found: set[str] = set()
    if isinstance(document, dict):
        for key, value in document.items():
            if key == "AI_LLM_PROVIDER":
                found.add(str(value))
            else:
                found |= _provider(value)
    elif isinstance(document, list):
        for item in document:
            if isinstance(item, str) and item.startswith("AI_LLM_PROVIDER="):
                found.add(item.partition("=")[2])
            else:
                found |= _provider(item)
    return found
