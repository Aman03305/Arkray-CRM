# Corrections of closed deals (adversarial review of 73ff1e3, M2; docs/pipeline.md#closed-deals):
# a won or lost deal's value and dates are frozen for every normal edit, and an administrator
# corrects them through their own operation, which appends one row here per correction (the
# figures before and after, which changed, why, who, when).
#
# A new, empty table: nothing is rewritten. Append-only like the stage history and the agreed
# price history: the trigger refuses UPDATE and DELETE from any role that can't disable it, and
# grant_app_privileges (which finds append-only tables by that trigger) grants the application
# role SELECT and INSERT only. An erasure redacts the free-text `reason` with the trigger
# disabled for that one statement, as it does the stage history's lost reasons
# (privacy.services.HISTORY_COLUMNS).
#
# Not reversible: dropping the table would delete every correction recorded since, so it
# refuses (RefuseReverse, last operation; docs/deployment.md#rollback).

import uuid
from decimal import Decimal

import django.contrib.postgres.fields
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from arkray.core.db import append_only_trigger
from arkray.core.migrations._reverse_guard import RefuseReverse


class Migration(migrations.Migration):

    dependencies = [
        ("pipeline", "0010_stage_reference_indexes"),
        ("core", "0002_forbid_mutation_function"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OpportunityCorrection",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                (
                    "fields",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(
                            choices=[
                                ("value", "Value"),
                                ("expected_close_date", "Expected close date"),
                                ("opportunity_date", "Opportunity date"),
                            ],
                            max_length=32,
                        ),
                        size=None,
                    ),
                ),
                ("value_before", models.DecimalField(decimal_places=2, max_digits=14)),
                ("value_after", models.DecimalField(decimal_places=2, max_digits=14)),
                ("expected_close_date_before", models.DateField(blank=True, null=True)),
                ("expected_close_date_after", models.DateField(blank=True, null=True)),
                ("opportunity_date_before", models.DateField()),
                ("opportunity_date_after", models.DateField()),
                (
                    "status",
                    models.CharField(
                        choices=[("open", "Open"), ("won", "Won"), ("lost", "Lost")],
                        max_length=8,
                    ),
                ),
                ("reason", models.CharField(blank=True, max_length=300)),
                ("opportunity_version", models.PositiveIntegerField()),
                ("occurred_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "actor",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "opportunity",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="pipeline.opportunity",
                    ),
                ),
                (
                    "subject_user",
                    models.ForeignKey(
                        blank=True,
                        db_index=False,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "pipeline_opportunity_correction",
                "indexes": [
                    models.Index(
                        models.F("opportunity"),
                        models.OrderBy(models.F("occurred_at"), descending=True),
                        models.OrderBy(models.F("id"), descending=True),
                        name="pipeline_correction_opp_idx",
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("fields__len__gte", 1),
                            (
                                "fields__contained_by",
                                ["value", "expected_close_date", "opportunity_date"],
                            ),
                        ),
                        name="pipeline_correction_fields_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("value_after__gte", Decimal("0")),
                            ("value_before__gte", Decimal("0")),
                        ),
                        name="pipeline_correction_value_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("status__in", ["won", "lost"])),
                        name="pipeline_correction_closed_only",
                    ),
                ],
            },
        ),
        append_only_trigger("pipeline_opportunity_correction"),
        RefuseReverse(
            "pipeline.0011_opportunity_corrections", "every correction of a closed deal since"
        ),
    ]
