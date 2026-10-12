"""Check a staging or production environment's dependencies, live and read-only, before it
takes traffic (config.preflight; docs/deployment.md#preflight).

    manage.py preflight                          every check but the edge's; exit 1 on any FAIL
    manage.py preflight --base-url https://crm.example.com     and the edge's
    manage.py preflight --json                   the same as JSON
    manage.py preflight --only database,redis    some groups (or --skip scanner)
    manage.py preflight --send-test-email ops@example.com      and send one email

Run it inside the backend image with the deployment's own environment, in the scanning
worker's container (worker-default): it alone reaches every dependency, ClamAV included
(elsewhere, `--skip scanner`). Its database role is the application's, so the role check
means what it says: with the migrate job's environment (the owner) that check fails by
design. Nothing is written but a probe object in the attachments' and exports' stores
(removed again), and an email only when asked; never a secret in its output.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser


def _groups(value: str) -> tuple[str, ...]:
    from config.preflight import GROUPS

    names = tuple(name.strip() for name in value.split(",") if name.strip())
    unknown = [name for name in names if name not in GROUPS]
    if unknown:
        raise CommandError(f"Unknown group {', '.join(unknown)}: one of {', '.join(GROUPS)}.")
    return names


class Command(BaseCommand):
    help = (
        "Check this environment's dependencies, live and read-only: PASS, WARN, FAIL or SKIP"
        " per check; exit 1 on any FAIL."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--json", action="store_true", help="print the results as JSON")
        parser.add_argument(
            "--only", default="", metavar="GROUPS", help="comma-separated groups to run"
        )
        parser.add_argument(
            "--skip", default="", metavar="GROUPS", help="comma-separated groups to leave out"
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=30.0,
            metavar="SECONDS",
            help="each group's deadline (30): a dependency that hangs costs one FAIL",
        )
        parser.add_argument(
            "--connections",
            type=int,
            default=None,
            metavar="N",
            help="the database connections the deployment plans (web processes x workers +"
            " Celery concurrency + beat), against max_connections",
        )
        parser.add_argument(
            "--send-test-email", default="", metavar="ADDRESS", help="also send one email there"
        )
        parser.add_argument(
            "--base-url", default="", metavar="URL", help="the public https:// URL: check the edge"
        )
        parser.add_argument(
            "--http-url",
            default="",
            metavar="URL",
            help="the edge's plain-HTTP URL, when it isn't http://<host> (port 80)",
        )
        parser.add_argument(
            "--edge-ca-file",
            default="",
            metavar="PATH",
            help="a private CA for the edge's certificate (a staging certificate)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from config import preflight

        if options["timeout"] <= 0:
            raise CommandError("--timeout must be positive.")
        only = _groups(options["only"]) or tuple(preflight.GROUPS)
        skip = set(_groups(options["skip"]))
        results = preflight.run(
            preflight.Options(
                groups=tuple(name for name in only if name not in skip),
                timeout_s=options["timeout"],
                send_test_email=options["send_test_email"],
                base_url=options["base_url"],
                http_url=options["http_url"],
                edge_ca_file=options["edge_ca_file"],
                connections=options["connections"] or preflight.PLANNED_CONNECTIONS,
            )
        )
        if options["json"]:
            self.stdout.write(preflight.as_json(results))
        else:
            self.stdout.write(preflight.as_table(results))
        failed = preflight.summary(results)[preflight.FAIL]
        if failed:
            raise CommandError(f"Preflight failed: {failed} check(s) FAIL.", returncode=1)
