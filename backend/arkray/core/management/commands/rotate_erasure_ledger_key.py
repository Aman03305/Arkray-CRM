"""Anchor the erasure ledger to a new key (docs/runbooks.md#rotate-the-erasure-ledger-key;
arkray.core.ledger.rotate_key).

    manage.py rotate_erasure_ledger_key --check
    manage.py rotate_erasure_ledger_key

Run it once every process signs with the new ERASURE_LEDGER_KEY and still accepts the old
one in ERASURE_LEDGER_VERIFY_KEYS. It verifies the whole chain, then appends a `key_rotated`
entry that names the new key by its id (never the key) and anchors every entry before it;
after that the old key may be removed from ERASURE_LEDGER_VERIFY_KEYS. Running it again is
harmless. Exit status: 0 done (or already done); 2 the ledger can't be read or verified
(nothing written).
"""

from __future__ import annotations

import sys
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from arkray.core import ledger


class Command(BaseCommand):
    help = "Anchor the erasure ledger to the current ERASURE_LEDGER_KEY (key rotation)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--check", action="store_true", help="verify the chain and report, change nothing"
        )

    def handle(self, *args: Any, check: bool, **options: Any) -> None:
        if not ledger.enabled():
            raise CommandError("No erasure ledger is configured (ERASURE_LEDGER_URL).")
        current = ledger.key_id(settings.ERASURE_LEDGER_KEY)
        try:
            entries, state = ledger.verified()
        except ledger.LedgerError as exc:
            self.stderr.write(f"The ledger can't be verified: {exc} Nothing was written.")
            sys.exit(2)
        self.stdout.write(
            f"{len(entries)} entries verified; current key {current}; last rotation to"
            f" {state.key or 'none'}; signed since: {', '.join(sorted(state.signers)) or 'none'}."
        )
        if check:
            return
        try:
            entry = ledger.rotate_key()
        except ledger.LedgerError as exc:
            self.stderr.write(f"The ledger can't be rotated: {exc} Nothing was written.")
            sys.exit(2)
        if entry is None:
            self.stdout.write("Already anchored to the current key: nothing to do.")
            return
        self.stdout.write(
            f"Entry {entry.seq} anchors the ledger to key {current}. Once every process runs"
            " with it, the old key can be removed from ERASURE_LEDGER_VERIFY_KEYS."
        )
