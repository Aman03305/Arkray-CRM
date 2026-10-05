# Product enhancement phase (docs/pipeline.md): user-defined pipelines, editable stages with a
# negotiation type, negotiated price history, the opportunity's customer and instrument
# details, and per-pipeline custom fields.
#
# Existing data survives unchanged in meaning:
# - the seeded "Sales Pipeline" becomes an organisation pipeline (owner NULL), still the
#   default; its "negotiation" stage becomes a negotiation stage (entering it from now on asks
#   for the negotiated price; deals already in it keep going);
# - every opportunity gets its opportunity date from its creation time (business day) and its
#   account and customer names from its lead (an editable snapshot from now on);
# - opportunities, values, owners, stage history, activities, notes and audit are untouched
#   (no version or updated_at changes: a backfill is not a user's edit).
#
# Table-wide (the backfill updates every opportunity), so it lifts the statement timeout for
# its own transaction first (tests/architecture/test_migrations.py).

import datetime
import uuid

import django.db.models.deletion
import django.db.models.expressions
import django.db.models.functions.text
import django.db.models.lookups
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import arkray.pipeline.models
from arkray.core.db import append_only_trigger


def backfill_opportunities(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE pipeline_opportunity AS o
               SET opportunity_date = (o.created_at AT TIME ZONE %s)::date,
                   account_name = LEFT(COALESCE(NULLIF(l.organization_name, ''), l.display_name), 200),
                   customer_name = LEFT(l.display_name, 200)
              FROM leads_lead AS l
             WHERE l.id = o.lead_id
            """,
            [settings.CRM_TIME_ZONE],
        )


MARK_NEGOTIATION = """
UPDATE pipeline_stage AS s
   SET is_negotiation = true
  FROM pipeline_pipeline AS p
 WHERE p.id = s.pipeline_id AND p.key = 'sales' AND s.key = 'negotiation' AND s.category = 'open'
"""


class Migration(migrations.Migration):

    dependencies = [
        ("leads", "0006_leads_autovacuum"),
        ("pipeline", "0005_search_indexes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunSQL("SET LOCAL statement_timeout = 0;", reverse_sql=migrations.RunSQL.noop),
        # --- pipelines: ownership, provenance, version ------------------------------------
        migrations.AlterField(
            model_name="pipeline",
            name="name",
            field=models.CharField(max_length=100),
        ),
        migrations.AddField(
            model_name="pipeline",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="pipeline",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                db_index=False,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="pipeline",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddIndex(
            model_name="pipeline",
            index=models.Index(
                models.F("owner"),
                models.F("name"),
                condition=models.Q(("owner__isnull", False)),
                name="pipeline_pipeline_owner_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="pipeline",
            constraint=models.CheckConstraint(
                condition=models.Q(("is_default", False), ("owner__isnull", True), _connector="OR"),
                name="pipeline_pipeline_default_is_shared",
            ),
        ),
        migrations.AddConstraint(
            model_name="pipeline",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)), name="pipeline_pipeline_version_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="pipeline",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("name"),
                condition=models.Q(("is_active", True), ("owner__isnull", True)),
                name="pipeline_pipeline_shared_name_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="pipeline",
            constraint=models.UniqueConstraint(
                models.F("owner"),
                django.db.models.functions.text.Lower("name"),
                condition=models.Q(("is_active", True), ("owner__isnull", False)),
                name="pipeline_pipeline_owner_name_unique",
            ),
        ),
        # --- stages: the negotiation type -------------------------------------------------
        migrations.AddField(
            model_name="stage",
            name="is_negotiation",
            field=models.BooleanField(default=False),
        ),
        migrations.RunSQL(MARK_NEGOTIATION, reverse_sql=migrations.RunSQL.noop),
        migrations.AddConstraint(
            model_name="stage",
            constraint=models.CheckConstraint(
                condition=models.Q(("is_negotiation", False), ("category", "open"), _connector="OR"),
                name="pipeline_stage_negotiation_is_open",
            ),
        ),
        # --- opportunities: the deal's details ----------------------------------------------
        migrations.AddField(
            model_name="opportunity",
            name="opportunity_date",
            field=models.DateField(default=arkray.pipeline.models.business_today),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="account_name",
            field=models.CharField(default="", max_length=200),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="opportunity",
            name="customer_name",
            field=models.CharField(default="", max_length=200),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="opportunity",
            name="contact_phone",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="contact_email",
            field=models.CharField(blank=True, default="", max_length=254),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="address",
            field=models.TextField(blank=True, default="", max_length=1000),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="instrument_name",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="work_load",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="custom_fields",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="negotiated_price",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="negotiated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_opportunities, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="opportunity",
            constraint=models.CheckConstraint(
                condition=models.Q(("opportunity_date__range", (datetime.date(2000, 1, 1), datetime.date(2099, 12, 31)))),
                name="pipeline_opp_date_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="opportunity",
            constraint=models.CheckConstraint(
                condition=models.Q(("account_name", ""), _negated=True),
                name="pipeline_opp_account_present",
            ),
        ),
        migrations.AddConstraint(
            model_name="opportunity",
            constraint=models.CheckConstraint(
                condition=models.Q(("customer_name", ""), _negated=True),
                name="pipeline_opp_customer_present",
            ),
        ),
        migrations.AddConstraint(
            model_name="opportunity",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("negotiated_at__isnull", True), ("negotiated_price__isnull", True)),
                    models.Q(("negotiated_at__isnull", False), ("negotiated_price__gte", 0)),
                    _connector="OR",
                ),
                name="pipeline_opp_negotiated_complete",
            ),
        ),
        migrations.AddConstraint(
            model_name="opportunity",
            constraint=models.CheckConstraint(
                condition=django.db.models.lookups.Exact(
                    django.db.models.expressions.Func(
                        models.F("custom_fields"),
                        function="jsonb_typeof",
                        output_field=models.TextField(),
                    ),
                    "object",
                ),
                name="pipeline_opp_custom_fields_object",
            ),
        ),
        # Plain (not CONCURRENTLY): this migration already holds the table for its backfill;
        # 0.3 s at 300,000 opportunities.
        migrations.AddIndex(
            model_name="opportunity",
            index=models.Index(
                models.F("owner"), models.F("pipeline"), name="pipeline_opp_owner_pipe_idx"
            ),
        ),
        # --- negotiated price history (append-only) -----------------------------------------
        migrations.CreateModel(
            name="NegotiationPrice",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("price", models.DecimalField(decimal_places=2, max_digits=14)),
                ("currency", models.CharField(max_length=3)),
                ("stage_name", models.CharField(max_length=50)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("stage_entry", "Entered a negotiation stage"),
                            ("revision", "Revised during negotiation"),
                            ("creation", "Created in a negotiation stage"),
                        ],
                        max_length=16,
                    ),
                ),
                ("opportunity_version", models.PositiveIntegerField()),
                ("support_session_id", models.UUIDField(blank=True, null=True)),
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
                    "stage",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="pipeline.stage",
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
                "db_table": "pipeline_negotiation_price",
                "indexes": [
                    models.Index(
                        models.F("opportunity"),
                        models.OrderBy(models.F("occurred_at"), descending=True),
                        models.OrderBy(models.F("id"), descending=True),
                        name="pipeline_negotiation_opp_idx",
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("price__gte", 0)),
                        name="pipeline_negotiation_price_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("source__in", ["stage_entry", "revision", "creation"])
                        ),
                        name="pipeline_negotiation_source_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("stage_name", ""), _negated=True),
                            ("currency__regex", "^[A-Z]{3}$"),
                        ),
                        name="pipeline_negotiation_complete",
                    ),
                ],
            },
        ),
        append_only_trigger("pipeline_negotiation_price"),
        # --- custom field definitions --------------------------------------------------------
        migrations.CreateModel(
            name="CustomField",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, editable=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=60)),
                (
                    "field_type",
                    models.CharField(
                        choices=[
                            ("text", "Text"),
                            ("long_text", "Long text"),
                            ("number", "Number"),
                            ("currency", "Currency (INR)"),
                            ("date", "Date"),
                            ("boolean", "Yes / no"),
                            ("single_select", "Single choice"),
                            ("multi_select", "Multiple choice"),
                        ],
                        max_length=16,
                    ),
                ),
                ("required", models.BooleanField(default=False)),
                ("options", models.JSONField(blank=True, default=list)),
                ("position", models.PositiveSmallIntegerField()),
                ("is_active", models.BooleanField(default=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        db_index=False,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "pipeline",
                    models.ForeignKey(
                        db_index=False,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="fields",
                        to="pipeline.pipeline",
                    ),
                ),
            ],
            options={
                "db_table": "pipeline_custom_field",
                "indexes": [
                    models.Index(
                        models.F("pipeline"), models.F("position"), name="pipeline_field_pipeline_idx"
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("name", ""), _negated=True),
                        name="pipeline_field_name_present",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            (
                                "field_type__in",
                                [
                                    "text",
                                    "long_text",
                                    "number",
                                    "currency",
                                    "date",
                                    "boolean",
                                    "single_select",
                                    "multi_select",
                                ],
                            )
                        ),
                        name="pipeline_field_type_valid",
                    ),
                    models.CheckConstraint(
                        condition=django.db.models.lookups.Exact(
                            django.db.models.expressions.Func(
                                models.F("options"),
                                function="jsonb_typeof",
                                output_field=models.TextField(),
                            ),
                            "array",
                        ),
                        name="pipeline_field_options_array",
                    ),
                    models.UniqueConstraint(
                        models.F("pipeline"),
                        django.db.models.functions.text.Lower("name"),
                        condition=models.Q(("is_active", True)),
                        name="pipeline_field_active_name_unique",
                    ),
                ],
            },
        ),
    ]
