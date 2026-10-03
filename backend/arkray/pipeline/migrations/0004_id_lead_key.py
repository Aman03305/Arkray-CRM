# Phase 4: the target of activities' (opportunity_id, lead_id) foreign key, which makes
# "an activity's lead is its opportunity's lead" a database invariant.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pipeline', '0003_seed_default_pipeline'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='opportunity',
            constraint=models.UniqueConstraint(fields=('id', 'lead'), name='pipeline_opportunity_id_lead_key'),
        ),
    ]
