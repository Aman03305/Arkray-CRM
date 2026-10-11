"""This process's view of the erasure ledger gate (core.ledger_gate), for container health
checks (final audit ARCH-1: workers that couldn't see the ledger looked healthy while they
skipped every task).

    manage.py ledger_status                       the gate's status; exit 1 unless open
    manage.py ledger_status --ping-worker ai      and the Celery worker ai@<this host> answers

The gate is compared afresh (the ledger read and verified, the database's applied position
queried): one short process, so probe it every minute or so, not every few seconds. With
`--ping-worker` the worker named `<name>@<hostname>` (Celery's `--hostname name@%h`) must
answer a ping through the broker within 5 s: a hung or dead worker turns unhealthy, and so
does every worker while the broker is down (none of them can work then). Prints the status
only, never a path or a key.
"""

from __future__ import annotations

import socket
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.core import ledger_gate

PING_TIMEOUT_S = 5.0


class Command(BaseCommand):
    help = "Print the erasure ledger gate's status; exit 1 unless it is open."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--ping-worker",
            metavar="NAME",
            help="also require the Celery worker NAME@<this host> to answer a ping",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        status = ledger_gate.GATE.status()
        self.stdout.write(f"erasure_ledger_gate={status}")
        if status not in ledger_gate.OPEN:
            raise CommandError(f"The erasure ledger gate is closed ({status}).")
        if options["ping_worker"]:
            from config.celery import app

            node = f"{options['ping_worker']}@{socket.gethostname()}"
            replies = app.control.ping(destination=[node], timeout=PING_TIMEOUT_S) or []
            if not any(node in reply for reply in replies):
                raise CommandError(f"The Celery worker {node} didn't answer a ping.")
            self.stdout.write(f"celery_worker={node} ok")
