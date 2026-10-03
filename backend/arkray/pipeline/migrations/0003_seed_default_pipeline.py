"""The default sales pipeline and its stages (docs/pipeline.md#stages).

Configuration rows, not demo data: every installation starts with them. Keys are immutable
identifiers; names, probabilities and positions are configuration that may change later
without touching opportunities or their history.
"""

from decimal import Decimal

from django.db import migrations

PIPELINE = ("sales", "Sales Pipeline")

STAGES = [
    # key, name, position, probability, category
    ("new", "New", 10, Decimal("10"), "open"),
    ("qualified", "Qualified", 20, Decimal("25"), "open"),
    ("proposal", "Proposal", 30, Decimal("50"), "open"),
    ("negotiation", "Negotiation", 40, Decimal("75"), "open"),
    ("won", "Won", 50, Decimal("100"), "won"),
    ("lost", "Lost", 60, Decimal("0"), "lost"),
]


def seed(apps, schema_editor):
    Pipeline = apps.get_model("pipeline", "Pipeline")
    Stage = apps.get_model("pipeline", "Stage")
    key, name = PIPELINE
    pipeline = Pipeline.objects.create(key=key, name=name, is_default=True)
    for stage_key, stage_name, position, probability, category in STAGES:
        Stage.objects.create(
            pipeline=pipeline,
            key=stage_key,
            name=stage_name,
            position=position,
            probability=probability,
            category=category,
        )


def unseed(apps, schema_editor):
    Pipeline = apps.get_model("pipeline", "Pipeline")
    Stage = apps.get_model("pipeline", "Stage")
    Stage.objects.filter(pipeline__key=PIPELINE[0]).delete()
    Pipeline.objects.filter(key=PIPELINE[0]).delete()


class Migration(migrations.Migration):
    dependencies = [("pipeline", "0002_integrity")]

    operations = [migrations.RunPython(seed, unseed)]
