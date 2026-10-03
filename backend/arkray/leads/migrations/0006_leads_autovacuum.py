# Phase 5 performance review, P1: keep leads_lead's visibility map and planner statistics
# fresh, so the dashboard's index-only lead figures stay index-only.
#
# Every lead edit is a non-HOT update (updated_at is indexed), which clears the all-visible
# bit of two heap pages. With PostgreSQL's defaults autovacuum waits for 20 % of the table to
# change and autoanalyze for 10 % (200,000 and 100,000 edits at 1,000,000 leads); meanwhile the
# planner still believes the pages are all-visible and the "index-only" count fetches a heap
# row per lead: the organisation figure measured up to ~1 s (60 ms freshly vacuumed), the
# heaviest owner's 115 ms (8 ms). At 1 % (10,000 edits per million leads) the map and the
# statistics are refreshed long before that (docs/dashboard.md#performance). ALTER TABLE ...
# SET (storage parameters) takes only SHARE UPDATE EXCLUSIVE: nothing waits for it.

from django.db import migrations

PARAMETERS = (
    "autovacuum_vacuum_scale_factor",
    "autovacuum_vacuum_insert_scale_factor",
    "autovacuum_analyze_scale_factor",
)


class Migration(migrations.Migration):

    dependencies = [
        ("leads", "0005_owner_index_covers_archive"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE leads_lead SET ({});".format(
                ", ".join(f"{name} = 0.01" for name in PARAMETERS)
            ),
            reverse_sql="ALTER TABLE leads_lead RESET ({});".format(", ".join(PARAMETERS)),
        ),
    ]
