"""Rebuild Ask Arkray's semantic index from the CRM (docs/operations.md#rebuild-the-rag-index).

    manage.py ai_reindex              index every source now (unchanged text is skipped)
    manage.py ai_reindex --rebuild    delete every chunk first, then index everything
    manage.py ai_reindex --enqueue    let the ai_index workers do it (reconciliation)

The index is derived data: deleting it loses nothing, the CRM is the source. While it is
rebuilt, semantic search finds fewer passages; nothing else is affected.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import connection

from arkray.ai import indexing
from arkray.ai.models import KnowledgeChunk


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
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = 0")  # an operator command, bounded batches
        try:
            self._index(rebuild=options["rebuild"])
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET statement_timeout")

    def _index(self, *, rebuild: bool) -> None:
        if rebuild:
            deleted, _ = KnowledgeChunk.objects.all().delete()
            self.stdout.write(f"Deleted {deleted} chunks.")

        def progress(module: Any, batch: int, totals: dict[Any, int]) -> None:
            done = ", ".join(f"{k.value}={v}" for k, v in sorted(totals.items()))
            self.stdout.write(f"  {module.value}: +{batch} sources ({done})")

        totals = indexing.rebuild(progress=progress)
        summary = ", ".join(f"{k.value}={v}" for k, v in sorted(totals.items()))
        self.stdout.write(f"Done: {summary or 'nothing to index'}.")
