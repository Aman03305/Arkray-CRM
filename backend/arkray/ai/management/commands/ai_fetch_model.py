"""Download and verify the embedding model's files into AI_EMBEDDING_MODEL_DIR (or check
them with --verify). The image build does this already (python -m arkray.ai.model_files)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.ai import model_files


class Command(BaseCommand):
    help = "Fetch (or --verify) the pinned embedding model files."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--verify", action="store_true", help="Only check the files.")

    def handle(self, *args: Any, **options: Any) -> None:
        directory = Path(settings.AI_EMBEDDING_MODEL_DIR)
        if not options["verify"]:
            model_files.fetch(directory)
        bad = model_files.verify(directory)
        if bad:
            raise CommandError(f"Missing or altered model files in {directory}: {', '.join(bad)}")
        self.stdout.write(f"Embedding model files verified in {directory}.")
