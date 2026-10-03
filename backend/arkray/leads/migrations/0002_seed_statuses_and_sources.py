"""The initial lead statuses and sources (docs/leads.md#statuses).

Configuration rows, not demo data: every installation starts with them. Keys are
immutable identifiers used by the API; names are display labels.
"""

from django.db import migrations

STATUSES = [
    # key, name, category, position, is_default
    ("new", "New", "open", 10, True),
    ("contacted", "Contacted", "open", 20, False),
    ("qualified", "Qualified", "qualified", 30, False),
    ("unqualified", "Unqualified", "unqualified", 40, False),
    ("converted", "Converted", "converted", 50, False),
]

SOURCES = [
    ("website", "Website", 10),
    ("referral", "Referral", 20),
    ("campaign", "Campaign", 30),
    ("cold_call", "Cold Call", 40),
    ("email", "Email", 50),
    ("event", "Event", 60),
    ("partner", "Partner", 70),
    ("other", "Other", 80),
]


def seed(apps, schema_editor):
    LeadStatus = apps.get_model("leads", "LeadStatus")
    LeadSource = apps.get_model("leads", "LeadSource")
    for key, name, category, position, is_default in STATUSES:
        LeadStatus.objects.create(
            key=key, name=name, category=category, position=position, is_default=is_default
        )
    for key, name, position in SOURCES:
        LeadSource.objects.create(key=key, name=name, position=position)


def unseed(apps, schema_editor):
    apps.get_model("leads", "LeadStatus").objects.filter(key__in=[s[0] for s in STATUSES]).delete()
    apps.get_model("leads", "LeadSource").objects.filter(key__in=[s[0] for s in SOURCES]).delete()


class Migration(migrations.Migration):
    dependencies = [("leads", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
