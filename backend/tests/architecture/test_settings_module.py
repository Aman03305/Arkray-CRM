"""A production process never falls back to the development settings (adversarial review of
73ff1e3, L14).

`manage.py` and `config/celery.py` default DJANGO_SETTINGS_MODULE to `config.settings.local`
(DEBUG, no database-role check), deliberately: `uv run python manage.py runserver` works on a
developer's machine without it (docs/development.md). `config/wsgi.py` defaults to production,
but `config/__init__.py` imports the Celery app first, so even gunicorn's default was the
development one. A cron job or a service unit without the variable silently ran with DEBUG
and without the role check.

The backend image now also sets ARKRAY_RUNTIME=production. With it, a process whose
DJANGO_SETTINGS_MODULE is unset or empty uses `config.settings.production`, and one that names
any other module is refused; any other ARKRAY_RUNTIME value is refused. Without it, nothing
changes for development.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
PRODUCTION = "config.settings.production"
LOCAL = "config.settings.local"
MARKER = "ARKRAY_RUNTIME"
PRINT_MODULE = "import os, config; print(os.environ['DJANGO_SETTINGS_MODULE'])"
WEAK_KEY = {
    "DJANGO_SECRET_KEY": "dev-only-short-key",
    "DATABASE_URL": "postgres://u:p@localhost:5432/db",
}


def _python(*args: str, **environ: str | None) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DJANGO_", "DATABASE_", "ARKRAY_", "ERASURE_", "METRICS_"))
    }
    for key, value in environ.items():
        if value is not None:
            env[key] = value
    return subprocess.run(  # noqa: S603 — fixed interpreter and arguments
        [sys.executable, *args],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def test_development_keeps_its_default():
    result = _python("-c", PRINT_MODULE)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [LOCAL]


@pytest.mark.parametrize("module", ["", None])
@pytest.mark.parametrize(
    "entry", ["config", "config.celery", "config.wsgi"], ids=["package", "celery", "wsgi"]
)
def test_the_production_runtime_defaults_to_production(entry, module):
    code = (
        f"import os, importlib; importlib.import_module({entry!r});"
        " print(os.environ['DJANGO_SETTINGS_MODULE'])"
    )
    # A key the development settings take and production's refuse: which one loaded shows.
    environ = {MARKER: "production", "DJANGO_SETTINGS_MODULE": module, **WEAK_KEY}
    if entry == "config.wsgi":
        # Serving loads the settings: they must be production's (refused here without a key).
        result = _python("-c", code, **environ)
        assert "DJANGO_SECRET_KEY must be a strong" in result.stderr, result.stderr[-1500:]
        return
    result = _python("-c", code, **environ)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [PRODUCTION]


def test_gunicorns_configuration_defaults_to_production_outside_the_image():
    """Without ARKRAY_RUNTIME and without the variable (a server started outside the image),
    loading gunicorn.conf.py as gunicorn does, then the application, loads the production
    settings (refused here for the weak key), never the development fallback that importing
    the `config` package used to apply first. Gunicorn itself doesn't import on Windows
    (fcntl): its one attribute the file sets is stubbed."""
    code = (
        "import importlib, runpy, sys, types;"
        "g = types.ModuleType('gunicorn'); h = types.ModuleType('gunicorn.http');"
        "w = types.ModuleType('gunicorn.http.wsgi'); g.http = h; h.wsgi = w;"
        "sys.modules.update({'gunicorn': g, 'gunicorn.http': h, 'gunicorn.http.wsgi': w});"
        "runpy.run_path('gunicorn.conf.py'); importlib.import_module('config.wsgi')"
    )
    result = _python("-c", code, **WEAK_KEY)
    assert "DJANGO_SECRET_KEY must be a strong" in result.stderr, result.stderr[-1500:]
    # An explicit module still wins (a developer serving the local settings on purpose).
    named = _python(
        "-c",
        code.replace(
            "importlib.import_module('config.wsgi')",
            "import os; print(os.environ['DJANGO_SETTINGS_MODULE'])",
        ),
        DJANGO_SETTINGS_MODULE=LOCAL,
    )
    assert named.returncode == 0, named.stderr
    assert named.stdout.split() == [LOCAL]


def test_a_management_command_in_the_production_runtime_loads_production_settings():
    """Without the variable, `manage.py check` there is production's check: refused for the
    missing secrets, never a quiet DEBUG run."""
    assert _python("manage.py", "check", **WEAK_KEY).returncode == 0  # development: fine
    result = _python("manage.py", "check", **{MARKER: "production"}, **WEAK_KEY)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert "DJANGO_SECRET_KEY must be a strong" in result.stderr


@pytest.mark.parametrize("module", [LOCAL, "config.settings.test", "config.settings.prod"])
def test_the_production_runtime_refuses_any_other_module(module):
    for args in (["manage.py", "check"], ["-c", PRINT_MODULE]):
        result = _python(*args, **{MARKER: "production", "DJANGO_SETTINGS_MODULE": module})
        assert result.returncode != 0, (args, result.stdout)
        assert MARKER in result.stderr
        assert module in result.stderr


@pytest.mark.parametrize("value", ["prod", "Production ", "true", "1"])
def test_an_unrecognised_runtime_is_refused(value):
    result = _python("-c", PRINT_MODULE, **{MARKER: value})
    assert result.returncode != 0
    assert MARKER in result.stderr


def test_the_image_declares_the_production_runtime():
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    runtime = dockerfile.rsplit("\nFROM ", 1)[1]
    assert re.search(rf"\b{MARKER}=production\b", runtime)
    assert re.search(rf"\bDJANGO_SETTINGS_MODULE={re.escape(PRODUCTION)}\b", runtime)


# --- review round 1 (INFRA-6) ------------------------------------------------------------------
@pytest.mark.parametrize("module", [LOCAL, "config.settings.test"])
def test_the_settings_option_cant_load_development_settings_in_the_production_runtime(module):
    """`manage.py --settings=...` sets the variable after the entry point chose: the
    development and test settings refuse to load in the production runtime themselves."""
    show = "from django.conf import settings; print(settings.SETTINGS_MODULE, settings.DEBUG)"
    result = _python(
        "manage.py",
        "shell",
        f"--settings={module}",
        "-c",
        show,
        **{MARKER: "production"},
        **WEAK_KEY,
    )
    assert result.returncode != 0, result.stdout
    assert "ImproperlyConfigured" in result.stderr
    assert MARKER in result.stderr
    imported = _python(
        "-c", f"import importlib; importlib.import_module({module!r})", **{MARKER: "production"}
    )
    assert imported.returncode != 0
    assert MARKER in imported.stderr
    # Without the runtime they load as before.
    assert (
        _python(
            "-c", f"import importlib; importlib.import_module({module!r})", **WEAK_KEY
        ).returncode
        == 0
    )


GUNICORN_STUB = (
    "import os, runpy, sys, types\n"
    "for name in ('gunicorn', 'gunicorn.http', 'gunicorn.http.wsgi'):\n"
    "    sys.modules[name] = types.ModuleType(name)\n"
    "sys.modules['gunicorn'].http = sys.modules['gunicorn.http']\n"
    "sys.modules['gunicorn.http'].wsgi = sys.modules['gunicorn.http.wsgi']\n"
    "runpy.run_path('gunicorn.conf.py')\n"
    "import config\n"
    "print(os.environ['DJANGO_SETTINGS_MODULE'])\n"
)


@pytest.mark.parametrize("module", ["", "  ", None])
def test_gunicorn_serves_production_settings_when_the_variable_is_empty_or_unset(module):
    """gunicorn.conf.py runs before the application loads: an EMPTY DJANGO_SETTINGS_MODULE
    (a unit file's `Environment=DJANGO_SETTINGS_MODULE=`) counts as unset there, so the
    Celery app's development fallback never applies to a web server (outside the image)."""
    result = _python("-c", GUNICORN_STUB, DJANGO_SETTINGS_MODULE=module)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [PRODUCTION]


def test_gunicorn_keeps_a_module_it_is_given():
    result = _python("-c", GUNICORN_STUB, DJANGO_SETTINGS_MODULE=LOCAL)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [LOCAL]
