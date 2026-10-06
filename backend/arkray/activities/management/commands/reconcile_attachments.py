"""Compare attachment rows with the objects in storage (docs/runbooks.md#attachment-reconciliation).

    manage.py reconcile_attachments                  check: read-only, changes nothing
    manage.py reconcile_attachments --json           the same, as JSON
    manage.py reconcile_attachments --repair         apply the safe repairs (audited)

Exit status: 0 healthy, 1 mismatches found, 2 errors (storage unreachable or failing: the
pass stops rather than report every file missing). The report names attachments by id
only; object keys of orphans appear only with --show-keys (operators only: a key is a
pointer to a customer's file).
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.activities import reconcile

_DURATION = re.compile(r"(\d+)([smhd]?)")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def duration(value: str) -> timedelta:
    """`90m`, `2h`, `1d` or plain seconds."""
    match = _DURATION.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError(value)
    return timedelta(seconds=int(match[1]) * _UNITS[match[2]])


class Command(BaseCommand):
    help = "Reconcile attachment rows with storage objects (read-only unless --repair)."

    def add_arguments(self, parser: CommandParser) -> None:
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--check", action="store_true", help="read-only (the default)")
        mode.add_argument(
            "--repair",
            action="store_true",
            help="fix what is safe: stale uploads, lost objects marked unavailable, leftover"
            " objects of deleted files purged; orphans are only reported",
        )
        parser.add_argument(
            "--stale-after",
            type=duration,
            default=reconcile.Options.stale_after,
            help="rows younger than this are in progress (default 1h, the housekeeping's)",
        )
        parser.add_argument(
            "--scan-stale-after",
            type=duration,
            default=reconcile.Options.scan_stale_after,
            help="a malware scan pending longer than this is reported (default 1h)",
        )
        parser.add_argument("--limit", type=int, help="check at most this many rows (partial)")
        parser.add_argument("--batch-size", type=int, default=reconcile.Options.batch_size)
        parser.add_argument(
            "--rate",
            type=float,
            default=reconcile.Options.rate,
            help="storage calls per second at most (default 50)",
        )
        parser.add_argument(
            "--verify-hash",
            action="store_true",
            help="also compare SHA-256 for a sample of files (downloads them)",
        )
        parser.add_argument("--hash-limit", type=int, default=reconcile.Options.hash_limit)
        parser.add_argument("--hash-sample", type=float, default=reconcile.Options.hash_sample)
        parser.add_argument(
            "--max-errors",
            type=int,
            default=reconcile.Options.max_errors,
            help="storage failures in a row before the pass stops (default 5)",
        )
        parser.add_argument(
            "--max-repair-missing",
            type=int,
            default=reconcile.Options.max_repair_missing,
            help="mark at most this many lost files unavailable in one run (default 50)",
        )
        parser.add_argument("--json", action="store_true", help="the report as JSON")
        parser.add_argument(
            "--show-keys",
            action="store_true",
            help="list orphaned object keys (operators only; never in tickets or chat)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["batch_size"] < 1 or (options["limit"] is not None and options["limit"] < 1):
            raise CommandError("--batch-size and --limit must be at least 1.")
        if not 0 <= options["hash_sample"] <= 1:
            raise CommandError("--hash-sample is a fraction between 0 and 1.")
        settings = reconcile.Options(
            repair=options["repair"],
            stale_after=options["stale_after"],
            scan_stale_after=options["scan_stale_after"],
            limit=options["limit"],
            batch_size=min(options["batch_size"], 1000),
            rate=options["rate"],
            verify_hash=options["verify_hash"],
            hash_limit=options["hash_limit"],
            hash_sample=options["hash_sample"],
            max_errors=max(options["max_errors"], 1),
            max_repair_missing=options["max_repair_missing"],
            show_keys=options["show_keys"],
        )
        report = reconcile.run(settings, progress=lambda line: self.stderr.write(line))
        if options["json"]:
            self.stdout.write(json.dumps(report.as_dict(), indent=2))
        else:
            self._print(report)
        code = report.exit_code()
        if code:
            raise SystemExit(code)

    def _print(self, report: reconcile.Report) -> None:
        scope = "complete" if report.complete else "partial"
        if report.aborted:
            scope = f"stopped: {report.aborted}"
        self.stdout.write(
            f"Attachment reconciliation ({report.mode}, {scope}):"
            f" {report.checked} rows, {report.listed} objects"
        )
        self.stdout.write(f"  {'healthy':<14}{report.healthy:>9}")
        for kind in reconcile.KINDS:
            count = getattr(report, kind)
            ids = report.samples.get(kind) or []
            shown = f"   e.g. {', '.join(ids)}" if ids else ""
            self.stdout.write(f"  {kind:<14}{count:>9}{shown}")
        self.stdout.write(f"  {'repaired':<14}{report.repaired:>9}")
        self.stdout.write(f"  {'errors':<14}{report.errors:>9}")
        for key in report.orphan_keys:
            self.stdout.write(f"  orphaned key: {key}")
        for note in report.notes:
            self.stdout.write(f"  note: {note}")
        verdict = {0: "healthy", 1: "mismatches found", 2: "errors: not fully checked"}
        self.stdout.write(f"Result: {verdict[report.exit_code()]} (exit {report.exit_code()})")
