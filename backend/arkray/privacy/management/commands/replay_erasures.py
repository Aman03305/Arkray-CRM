"""Re-apply the erasure ledger after a database restore, before traffic resumes
(docs/runbooks.md#restore-from-backup; arkray.privacy.replay).

    manage.py replay_erasures --by admin@example.com --report restore-report.json --dry-run
    manage.py replay_erasures --by admin@example.com --report restore-report.json

Run with the schema owner's database credentials (erasure redacts the append-only history).
Exit status: 0 complete (the API reopens within a minute); 1 an entry failed (the API stays
closed: fix and rerun); 2 the ledger can't be read or was tampered with (nothing changed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.core import ledger
from arkray.identity.models import User, normalize_email
from arkray.identity.policy import Capability, has_capability
from arkray.privacy import replay


class Command(BaseCommand):
    help = "Re-apply the erasure ledger after a restore (writes a reconciliation report)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--by", required=True, help="the administrator's email")
        parser.add_argument("--report", required=True, help="where to write the JSON report")
        parser.add_argument("--dry-run", action="store_true", help="check only, change nothing")

    def handle(self, *args: Any, by: str, report: str, dry_run: bool, **options: Any) -> None:
        operator = User.objects.filter(email=normalize_email(by)).first()
        if operator is None or not has_capability(operator, Capability.CRM_MANAGE_ANY):
            raise CommandError(f"--by must name an active administrator ({by!r} isn't one).")
        if not ledger.enabled():
            raise CommandError("No erasure ledger is configured (ERASURE_LEDGER_URL).")
        try:
            result = replay.replay(operator=operator, dry_run=dry_run)
        except ledger.LedgerError as exc:
            self.stderr.write(f"The ledger can't be used: {exc} Nothing was changed.")
            sys.exit(2)
        Path(report).write_text(json.dumps(result.as_dict(), indent=2), encoding="utf-8")
        counts = ", ".join(f"{k}: {v}" for k, v in sorted(result.counts().items())) or "none"
        self.stdout.write(
            f"{result.ledger_entries} ledger entries ({counts}); database at entry"
            f" {result.applied_after} of {result.ledger_head_seq}. Report: {report}."
        )
        if dry_run:
            self.stdout.write("Dry run: nothing changed; the API stays closed until a real run.")
            return
        if not result.complete:
            self.stderr.write("Some entries failed (see the report): the API stays closed.")
            sys.exit(1)
        self.stdout.write("Complete: the API reopens within ERASURE_LEDGER_CHECK_INTERVAL_S.")
