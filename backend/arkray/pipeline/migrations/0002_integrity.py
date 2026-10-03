"""Database-enforced pipeline invariants that Django can't declare (docs/pipeline.md).

1. An opportunity's status *is* its stage's category, and its pipeline its stage's pipeline:
   (stage_id, pipeline_id, status) references the stage's (id, pipeline_id, category). The
   status can't be edited on its own, and a stage's category or pipeline can't change
   while any opportunity (open or closed, archived or not) uses it.
2. An open opportunity is owned by its lead's owner: (lead_id, open_owner_id) references
   the lead's (id, owner_id). `open_owner_id` is NULL for won/lost opportunities, which the
   key then ignores (MATCH SIMPLE): they keep the owner who closed them. Checked at commit
   (DEFERRABLE INITIALLY DEFERRED), because a reassignment updates the lead first and moves
   its open opportunities afterwards, in the same transaction.
3. Stage history is append-only (a trigger refuses UPDATE and DELETE).
"""

from django.db import migrations

from arkray.core.db import append_only_trigger

STAGE_KEY = "pipeline_opportunity_stage_category_fk"
OWNER_KEY = "pipeline_opportunity_open_owner_fk"


class Migration(migrations.Migration):
    dependencies = [
        ("pipeline", "0001_initial"),
        ("leads", "0004_id_owner_key"),
        ("core", "0002_forbid_mutation_function"),
    ]

    operations = [
        migrations.RunSQL(
            sql=f"""
                ALTER TABLE pipeline_opportunity ADD CONSTRAINT {STAGE_KEY}
                    FOREIGN KEY (stage_id, pipeline_id, status)
                    REFERENCES pipeline_stage (id, pipeline_id, category);
            """,
            reverse_sql=f"ALTER TABLE pipeline_opportunity DROP CONSTRAINT {STAGE_KEY};",
        ),
        migrations.RunSQL(
            sql=f"""
                ALTER TABLE pipeline_opportunity ADD CONSTRAINT {OWNER_KEY}
                    FOREIGN KEY (lead_id, open_owner_id)
                    REFERENCES leads_lead (id, owner_id)
                    DEFERRABLE INITIALLY DEFERRED;
            """,
            reverse_sql=f"ALTER TABLE pipeline_opportunity DROP CONSTRAINT {OWNER_KEY};",
        ),
        append_only_trigger("pipeline_stage_history"),
    ]
