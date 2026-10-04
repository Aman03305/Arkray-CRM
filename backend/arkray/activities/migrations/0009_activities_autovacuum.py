# Phase 10 database review: keep activities_activity's visibility map fresh, as leads.0006
# does for leads, so the dashboard's activity figures (open tasks, meetings from today on:
# index-only ranges of the schedule indexes) stay index-only.
#
# New tasks and meetings are inserts on pages that aren't all-visible until a vacuum, and
# they are exactly the rows those ranges read. With PostgreSQL's defaults the insert-vacuum
# waits for 20 % of the table (400,000 inserts at 2,000,000 activities). Measured on the
# benchmark copy after the Phase 10 load tests, 5,947 inserts since the last vacuum: the
# organisation's "meetings from today" range already needed 4,309 heap fetches for 31,263
# rows, the heaviest owner's 285 for 1,996 (14 %); between default vacuums that approaches
# every recent row. At 2 % (40,000 inserts at 2,000,000) the map stays close to complete; an
# insert-only vacuum skips index cleanup, so even with the trigram indexes it is cheap.
# ALTER TABLE ... SET (storage parameters) takes only SHARE UPDATE EXCLUSIVE.

from django.db import migrations

PARAMETERS = (
    "autovacuum_vacuum_scale_factor",
    "autovacuum_vacuum_insert_scale_factor",
    "autovacuum_analyze_scale_factor",
)


class Migration(migrations.Migration):
    dependencies = [
        ("activities", "0008_search_indexes"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE activities_activity SET ({});".format(
                ", ".join(f"{name} = 0.02" for name in PARAMETERS)
            ),
            reverse_sql="ALTER TABLE activities_activity RESET ({});".format(", ".join(PARAMETERS)),
        ),
    ]
