# Agreed price and CPT on entering negotiation (ADR-0029): the agreed CPT recorded with each
# agreed (negotiated) price in the append-only history (docs/pipeline.md#negotiation). The
# opportunity has no copy of it: the latest is read from the newest history row.
#
# Existing rows get an empty value: prices recorded before the agreed CPT was asked for have
# none, and nothing is guessed for them. Adding a column with a constant default is a
# catalogue-only change in PostgreSQL 11+ (no table rewrite, no backfill; the append-only
# trigger on pipeline_negotiation_price fires on UPDATE/DELETE, never on ALTER TABLE). The
# column KEEPS its database default (`db_default`), so the previous release, which never
# names it, can still insert during a rolling deploy.
#
# Not reversible: dropping the column would delete the agreed CPT of every price recorded
# since, so it refuses (RefuseReverse, last operation; docs/deployment.md#rollback).

from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse


class Migration(migrations.Migration):

    dependencies = [
        ('pipeline', '0008_opportunity_expected_cpt'),
    ]

    operations = [
        migrations.AddField(
            model_name='negotiationprice',
            name='agreed_cpt',
            field=models.CharField(blank=True, db_default='', default='', max_length=100),
        ),
        RefuseReverse("pipeline.0009_agreed_cpt", "the agreed CPT of every negotiated price"),
    ]
