# Deep security audit M-1: when a lead was erased on request (privacy.services.erase) is
# recorded on the lead (`erased_at`), instead of being inferred from the placeholder text the
# erasure leaves, which a restore and an edit undid. An erased lead can no longer be restored
# or edited, nor its deals' and activities' personal text written again (docs/privacy.md#
# erasure).
#
# Leads erased before this release are marked from what the erasure left:
# - the `lead.erased` audit event (the time of the first, as erase() keeps the first erasure's
#   time when it runs again), which survives a restore and a
#   refill of the lead (the audit trail is append-only; such a lead can then be erased again,
#   removing what was re-entered);
# - otherwise the placeholder rule the erasure command used to recognise an erased lead
#   (first name "[erased]", every other personal field empty), dated by its archive time
#   (an erasure archives the lead) or, failing that, its last change.
# Neither touches `version` or `updated_at`: a backfill is not a user's edit.
#
# The new column is nullable with no default: adding it is a catalogue-only change, and the
# previous release, which never names it, can still insert during a rolling deploy. The
# backfill reads every lead (and the audit trail's `lead.erased` events, by their index), so
# it lifts the statement timeout for its own transaction first
# (tests/architecture/test_migrations.py).
#
# Not reversible: dropping the column would forget which leads were erased (and so let them be
# restored and refilled), so it refuses (RefuseReverse, last operation;
# docs/deployment.md#rollback).

from django.db import migrations, models

from arkray.core.migrations._reverse_guard import RefuseReverse

ERASED = "[erased]"
EMPTY = (
    "last_name",
    "organization_name",
    "job_title",
    "email",
    "phone",
    "mobile",
    "alternate_phone",
    "address_line_1",
    "address_line_2",
    "city",
    "state",
    "postal_code",
    "country",
    "description",
)

FROM_AUDIT = """
UPDATE leads_lead AS l
   SET erased_at = e.at
  FROM (
        SELECT target_id::uuid AS lead_id, min(occurred_at) AS at
          FROM audit_event
         WHERE action = 'lead.erased' AND target_type = 'lead'
           AND target_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
         GROUP BY target_id
       ) AS e
 WHERE l.id = e.lead_id AND l.erased_at IS NULL
"""

FROM_PLACEHOLDER = (
    "UPDATE leads_lead SET erased_at = COALESCE(archived_at, updated_at)"
    " WHERE erased_at IS NULL AND first_name = %s AND "
    + " AND ".join(f"{column} = ''" for column in EMPTY)
)


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0002_audit_event_append_only"),
        ("leads", "0006_leads_autovacuum"),
    ]

    operations = [
        migrations.RunSQL(
            sql="SET LOCAL statement_timeout = 0;", reverse_sql=migrations.RunSQL.noop
        ),
        migrations.AddField(
            model_name="lead",
            name="erased_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunSQL(
            sql=[FROM_AUDIT, (FROM_PLACEHOLDER, [ERASED])], reverse_sql=migrations.RunSQL.noop
        ),
        RefuseReverse("leads.0007_lead_erased_at", "which leads were erased on request"),
    ]
