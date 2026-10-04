"""Give the application role its privileges (docs/security.md#database-privileges).

    manage.py grant_app_privileges arkray_app

Run as the schema owner right after `migrate` in every release (new tables need grants;
append-only tables are found by their trigger). Idempotent.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection, transaction

from arkray.core import privileges


class Command(BaseCommand):
    help = "Grant the application database role exactly what it needs (run as the owner)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("role", help="the application role, for example arkray_app")

    def handle(self, *args: Any, role: str, **options: Any) -> None:
        try:
            with transaction.atomic():
                statements = privileges.grant(connection, role)
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(f"{len(statements)} statements applied for role {role}.")
