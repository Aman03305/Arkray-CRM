# Opportunity/lead flow change (ADR-0028): the opportunity's Expected CPT, optional free text
# (docs/pipeline.md#expected-cpt).
#
# Existing opportunities get an empty value. Adding a column with a constant default is a
# catalogue-only change in PostgreSQL 11+ (no table rewrite, no backfill), so it takes a
# moment at any size. The column KEEPS its database default (`db_default`): the previous
# release never names the column, and must still be able to insert opportunities while it
# runs beside this one in a rolling deploy, or after a rollback to it (backend review, P2).
# Reverse drops the column again. Nothing else changes: titles, instruments, leads and
# history are untouched.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pipeline', '0007_search_customer_names'),
    ]

    operations = [
        migrations.AddField(
            model_name='opportunity',
            name='expected_cpt',
            field=models.CharField(blank=True, db_default='', default='', max_length=100),
        ),
    ]
