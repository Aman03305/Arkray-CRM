# Adversarial review of 73ff1e3, M1: deleting an Ask Arkray conversation answered 409
# `legal_hold` when a hold kept it and 204 otherwise, which told its asker whether any
# customer it named (anyone's, in the whole organisation) was under a legal hold, and a held
# user that they were held. A deletion now always answers 204; a conversation a hold keeps is
# kept as a tombstone instead (`forgotten_at`): never shown, continued or answered again, and
# deleted by the hourly housekeeping once no hold keeps it (docs/privacy.md#legal-holds).
#
# The new column is nullable with no default: adding it is a catalogue-only change (no table
# rewrite, no backfill: every existing conversation is a live one), and the previous release,
# which never names it, can still insert during a rolling deploy (its rows are NULL: live).
# It doesn't know tombstones, though: until its pods are replaced it would list one the new
# release made, and answer deleting it as before (docs/deployment.md#which-previous-release-
# can-run-beside-the-new-schema).
#
# Not reversible: dropping the column would turn every tombstone back into a conversation
# its asker sees again, so it refuses (RefuseReverse, last operation;
# docs/deployment.md#rollback).

from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0003_question_finished_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="conversation",
            name="forgotten_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        # Last, so it runs first when reversing (docs/deployment.md#rollback).
        RefuseReverse(
            "ai.0004_conversation_forgotten_at",
            "which Ask Arkray conversations their askers deleted while a legal hold kept them"
            " (they would be shown to them again)",
        ),
    ]
