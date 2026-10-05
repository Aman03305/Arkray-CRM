# ADR-0027: with Leads gone from the UI, global search matches an opportunity's customer
# snapshot (account and customer names) as well as its title (pipeline.models.SEARCH_TEXT).
# The new trigram index is built before the old one is dropped, so search is served by an
# index throughout.
#
# Both steps run CONCURRENTLY, so opportunity reads and writes go on meanwhile; that can't run
# in a transaction, so the migration isn't atomic and lifts the timeouts for its session (not
# SET LOCAL), restoring them at the end in either direction (as pipeline.0005).

import django.contrib.postgres.indexes
import django.db.models.functions.text
from django.contrib.postgres.operations import AddIndexConcurrently, RemoveIndexConcurrently
from django.db import migrations, models

LIFT = "SET statement_timeout = 0; SET lock_timeout = 0;"
RESTORE = "RESET statement_timeout; RESET lock_timeout;"


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('pipeline', '0006_ownership_negotiation_fields'),
    ]

    operations = [
        migrations.RunSQL(sql=LIFT, reverse_sql=RESTORE),
        AddIndexConcurrently(
            model_name='opportunity',
            index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper(django.db.models.functions.text.Concat('title', models.Value(' '), 'account_name', models.Value(' '), 'customer_name')), name='gin_trgm_ops'), condition=models.Q(('archived_at__isnull', True)), name='pipeline_opp_text_trgm'),
        ),
        RemoveIndexConcurrently(
            model_name='opportunity',
            name='pipeline_opp_search_trgm',
        ),
        migrations.RunSQL(sql=RESTORE, reverse_sql=LIFT),
    ]
