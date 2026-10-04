"""Deploy checks: `manage.py check --deploy --database default`, run in the release
pipeline with the application's credentials (docs/deployment.md#release-process). Not a
plain database check: `migrate` runs those, as the owner, on purpose."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.checks import CheckMessage, Error, Tags, register
from django.core.checks import Warning as CheckWarning
from django.db import connections

from . import privileges

HINT = "Connect as the application role (docs/security.md#database-privileges)."


@register(Tags.database, deploy=True)
def database_role(app_configs: Any = None, databases: Any = None, **_: Any) -> list[CheckMessage]:
    """R74 and R63: the runtime role and PostgreSQL's statement logging."""
    messages: list[CheckMessage] = []
    level = Error if settings.DB_REQUIRE_RESTRICTED_ROLE else CheckWarning
    for alias in databases or []:
        with connections[alias].cursor() as cursor:
            for problem in privileges.runtime_problems(cursor):
                messages.append(
                    level(
                        f"The database role of {alias!r} may not run the application: {problem}.",
                        hint=HINT,
                        id="arkray.E001",
                    )
                )
            if privileges.logs_failed_statements(cursor):
                messages.append(
                    CheckWarning(
                        "PostgreSQL logs failing statements or failing rows, values included.",
                        hint=(
                            "Set log_min_error_statement = panic and log_error_verbosity = terse"
                            " (R63, docs/deployment.md)."
                        ),
                        id="arkray.W002",
                    )
                )
    return messages
