"""Rebuild Ask Arkray's semantic index from the CRM (docs/operations.md#rebuild-the-rag-index).

    manage.py ai_reindex              index every source now (unchanged text is skipped)
    manage.py ai_reindex --rebuild    delete every chunk first, then index everything
    manage.py ai_reindex --enqueue    let the ai_index workers do it (reconciliation)

The index is derived data: deleting it loses nothing, the CRM is the source. While it is
rebuilt, semantic search finds fewer passages; nothing else is affected.

Nothing here changes the session (final audit P3: `SET statement_timeout = 0` outlived the
command behind a transaction-mode pooler, on whichever server connection ran it, for its
other clients, and the `RESET` could run on another). The one unbounded statement, the
`--rebuild` delete, lifts the timeout for its own transaction only (`SET LOCAL`); every other
statement is one bounded batch and runs under the connection's usual timeout.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import connection, transaction

from arkray.ai import indexing
from arkray.ai.models import KnowledgeChunk


def delete_all_chunks() -> int:
    """Empty the index in one transaction, its statement timeout lifted for that transaction
    alone (safe behind a transaction pooler)."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = 0")
        deleted, _ = KnowledgeChunk.objects.all().delete()
    return deleted


class Command(BaseCommand):
    help = "Rebuild the Ask Arkray semantic index from the CRM records."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--rebuild", action="store_true", help="Delete all chunks first.")
        parser.add_argument(
            "--enqueue", action="store_true", help="Queue reconciliation for the workers."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if options["enqueue"]:
            indexing.start_reconciliation()
            self.stdout.write("Reconciliation queued (ai_index workers).")
            return
        if options["rebuild"]:
            self.stdout.write(f"Deleted {delete_all_chunks()} chunks.")

        def progress(module: Any, batch: int, totals: dict[Any, int]) -> None:
            done = ", ".join(f"{k.value}={v}" for k, v in sorted(totals.items()))
            self.stdout.write(f"  {module.value}: +{batch} sources ({done})")

        totals = indexing.rebuild(progress=progress)
        summary = ", ".join(f"{k.value}={v}" for k, v in sorted(totals.items()))
        self.stdout.write(f"Done: {summary or 'nothing to index'}.")
