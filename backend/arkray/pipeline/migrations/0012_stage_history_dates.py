# The deal's dates on every stage-history row (review round 1 of the 73ff1e3 remediation,
# DEALS-2; docs/pipeline.md#stage-history): a won or lost deal's figures are frozen, and
# reopening it is the salesperson's way to change them. Its value and probability were kept
# with each transition, its expected close date and opportunity date nowhere (the update's
# audit names fields only), so reopen, edit and close again lost the dates it was won at. Now
# each new row keeps them beside the value and probability.
#
# Two nullable columns without a DEFAULT clause: PostgreSQL fills NULL for any INSERT that
# doesn't name them, so the previous release (which never does) can still write history during
# a rolling deploy or after a rollback; a constant-free nullable ADD COLUMN is catalogue-only
# (no rewrite of the history, and the append-only trigger fires on UPDATE/DELETE, never on
# ALTER TABLE). Existing rows keep NULL: what their dates were isn't known, and nothing is
# guessed.
#
# Not reversible: dropping the columns would delete the dates recorded since, so it refuses
# (RefuseReverse, last operation; docs/deployment.md#rollback).

from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0011_opportunity_corrections"),
    ]

    operations = [
        migrations.AddField(
            model_name="stagehistory",
            name="expected_close_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="stagehistory",
            name="opportunity_date",
            field=models.DateField(blank=True, null=True),
        ),
        RefuseReverse(
            "pipeline.0012_stage_history_dates", "the deal dates recorded in the stage history since"
        ),
    ]
