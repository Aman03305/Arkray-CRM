"""The reverse-migration guard (docs/deployment.md#rollback, docs/database.md#reversibility).

Every migration after the supported previous release (v1.0 RC, 64bb641) ends with one. Most
of their reverses would drop what people recorded after the upgrade (negotiated price
history, agreed and Expected CPT, user pipelines, custom fields, support sessions, note
edits, attachments), so they refuse instead: the way back is the database backup taken before
the upgrade, or a forward fix. The two index-only ones refuse too, so the rule has no
exceptions and a refused rollback never stops part-way. Operations are reversed in reverse
order, so the guard, the LAST operation, runs before anything else is undone, and a refused
rollback changes nothing (tests/architecture/test_migrations.py,
tests/integration/test_upgrade_from_release.py).

Not a migration itself: the loader skips modules whose names start with "_".
"""

from typing import Any, NoReturn

from django.db import migrations
from django.db.migrations.exceptions import IrreversibleError

ROLLBACK_DOCS = "docs/deployment.md#rollback"
NO_DATA = (
    "no migration after the previous release (v1.0 RC) can be, so a rollback never stops part-way"
)


class RefuseReverse(migrations.RunPython):
    """Nothing forwards; backwards, raises IrreversibleError before anything is undone.

    `loses`: what reversing would delete, or None for a migration that holds no data."""

    def __init__(self, migration: str, loses: str | None = None) -> None:
        self.migration = migration
        self.loses = loses
        super().__init__(migrations.RunPython.noop, reverse_code=self.refuse, elidable=False)

    def refuse(self, apps: Any, schema_editor: Any) -> NoReturn:
        reason = f"that would delete {self.loses}" if self.loses else NO_DATA
        raise IrreversibleError(
            f"{self.migration} can't be reversed: {reason}. To go back to the previous "
            "release, restore the database backup taken before the upgrade (with that "
            f"release), or deploy a forward fix; see {ROLLBACK_DOCS}."
        )

    def describe(self) -> str:
        return f"Refuse to reverse {self.migration}"
