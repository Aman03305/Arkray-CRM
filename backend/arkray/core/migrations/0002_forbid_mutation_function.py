from django.db import migrations

from arkray.core.db import create_forbid_mutation_function


class Migration(migrations.Migration):
    """Shared trigger function used by every append-only table (audit, history, timeline)."""

    dependencies = [("core", "0001_initial")]

    operations = [create_forbid_mutation_function()]
