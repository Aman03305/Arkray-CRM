"""Re-encrypt every authenticator secret under the current DJANGO_SECRET_KEY (ADR-0033).

    manage.py mfa_reencrypt

Run after rotating the key (the old one in DJANGO_SECRET_KEY_FALLBACKS) and before removing
the old one from the fallbacks: a secret still under a removed key can't be read, and its
owner needs `reset_mfa`. Idempotent; exits non-zero if any secret is unreadable.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from arkray.identity import mfa_services


class Command(BaseCommand):
    help = "Re-encrypt every two-step verification secret under the current secret key."

    def handle(self, *args: Any, **options: Any) -> None:
        done, unreadable = mfa_services.reencrypt_all()
        self.stdout.write(f"Re-encrypted {done} authenticator secret(s).")
        if unreadable:
            raise CommandError(
                f"{unreadable} secret(s) are under no configured key: put the old key back in"
                " DJANGO_SECRET_KEY_FALLBACKS and run this again, or reset those accounts"
                " with reset_mfa."
            )
