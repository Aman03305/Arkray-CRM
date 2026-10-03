from django.db import migrations

from arkray.core.db import append_only_trigger


class Migration(migrations.Migration):
    """Audit events can be inserted but never updated or deleted — enforced by PostgreSQL."""

    dependencies = [
        ("audit", "0001_initial"),
        ("core", "0002_forbid_mutation_function"),
    ]

    operations = [append_only_trigger("audit_event")]
