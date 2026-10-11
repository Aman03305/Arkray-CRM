# Audit DOMAIN-3: the stage references had no index. Whether a stage being removed is still
# referenced (configuration._stage_referenced: any opportunity, archived ones included, any
# transition into or out of it, any agreed price recorded in it) read whole tables: a cold
# EXISTS on pipeline_opportunity.stage_id took about 1.5 s at 300,000 opportunities (the
# board's stage indexes are partial: archived deals and the statuses aren't in them). The
# same columns are what the foreign-key checks and Django's PROTECT queries read when a stage
# is deleted, and (the opportunity's composite key) when one is retyped. One plain index per
# referencing column, nothing more (docs/database.md).
#
# Built CONCURRENTLY, so reads and writes go on throughout; that can't run in a transaction,
# so the migration isn't atomic and lifts the timeouts for its session (not SET LOCAL),
# restoring them at the end in either direction (as leads.0005 and pipeline.0007).
#
# Holds no data of its own, but refuses to be reversed like every migration after the
# previous release (RefuseReverse, last operation; docs/deployment.md#rollback).

from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse

LIFT = "SET statement_timeout = 0; SET lock_timeout = 0;"
RESTORE = "RESET statement_timeout; RESET lock_timeout;"


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("pipeline", "0009_agreed_cpt"),
    ]

    operations = [
        migrations.RunSQL(sql=LIFT, reverse_sql=RESTORE),
        AddIndexConcurrently(
            model_name="opportunity",
            index=models.Index(models.F("stage"), name="pipeline_opp_stage_idx"),
        ),
        AddIndexConcurrently(
            model_name="stagehistory",
            index=models.Index(models.F("to_stage"), name="pipeline_hist_to_stage_idx"),
        ),
        AddIndexConcurrently(
            model_name="stagehistory",
            index=models.Index(models.F("from_stage"), name="pipeline_hist_from_stage_idx"),
        ),
        AddIndexConcurrently(
            model_name="negotiationprice",
            index=models.Index(models.F("stage"), name="pipeline_negotiation_stage_idx"),
        ),
        migrations.RunSQL(sql=RESTORE, reverse_sql=LIFT),
        RefuseReverse("pipeline.0010_stage_reference_indexes"),
    ]
