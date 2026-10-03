"""Database-enforced activity invariants that Django can't declare (docs/activities.md).

1. An activity linked to an opportunity has that opportunity's lead: (opportunity_id,
   lead_id) references the opportunity's (id, lead_id). MATCH SIMPLE: activities linked to
   the lead alone (opportunity_id NULL) are exempt.
2. Current work is owned by its lead's owner: (lead_id, current_owner_id) references the
   lead's (id, owner_id). `current_owner_id` is the owner of an open task, a scheduled
   meeting or a note, and NULL for completed or cancelled ones, which keep the owner who had
   them. Checked at commit (DEFERRABLE INITIALLY DEFERRED): a reassignment updates the lead
   first and moves its current activities afterwards, in the same transaction (ADR-0018's
   mechanism, extended).
3. A timeline entry about an activity or an opportunity is filed under that record's lead.
4. The timeline is append-only (a trigger refuses UPDATE and DELETE, even from raw SQL).
"""

from django.db import migrations

from arkray.core.db import append_only_trigger

FOREIGN_KEYS = {
    "activities_activity": [
        (
            "activities_activity_opportunity_lead_fk",
            "(opportunity_id, lead_id) REFERENCES pipeline_opportunity (id, lead_id)",
        ),
        (
            "activities_activity_current_owner_fk",
            "(lead_id, current_owner_id) REFERENCES leads_lead (id, owner_id)"
            " DEFERRABLE INITIALLY DEFERRED",
        ),
    ],
    "activities_timeline_entry": [
        (
            "activities_timeline_activity_lead_fk",
            "(activity_id, lead_id) REFERENCES activities_activity (id, lead_id)",
        ),
        (
            "activities_timeline_opportunity_lead_fk",
            "(opportunity_id, lead_id) REFERENCES pipeline_opportunity (id, lead_id)",
        ),
    ],
}


class Migration(migrations.Migration):
    dependencies = [
        ("activities", "0001_initial"),
        ("leads", "0004_id_owner_key"),
        ("pipeline", "0004_id_lead_key"),
        ("core", "0002_forbid_mutation_function"),
    ]

    operations = [
        *(
            migrations.RunSQL(
                sql=f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY {definition};",
                reverse_sql=f"ALTER TABLE {table} DROP CONSTRAINT {name};",
            )
            for table, keys in FOREIGN_KEYS.items()
            for name, definition in keys
        ),
        append_only_trigger("activities_timeline_entry"),
    ]
