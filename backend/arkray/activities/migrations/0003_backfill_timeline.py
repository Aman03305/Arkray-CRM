"""Timelines for leads and opportunities that existed before Phase 4, from the authoritative
history the earlier phases kept: the audit trail (lead created, status changed, reassigned,
archived, restored; every lead service audits in the same transaction as its change) and
the opportunities' append-only stage history (created, every stage change, won, lost,
reopened). Nothing is invented: a lead without such history gets no rows.

Snapshots match what the live subscribers record (arkray.activities.timeline). Status names
are the current names of the recorded keys (the audit trail kept keys only); stage names
are the copies the stage history made at the time.

Runs only into an empty table, so it can never duplicate history. The table is append-only,
so there is nothing to reverse: unapplying 0001 drops it.
"""

from django.db import migrations

BACKFILL = """
INSERT INTO activities_timeline_entry
    (lead_id, opportunity_id, activity_id, kind, actor_id, occurred_at, data)
SELECT lead_id, opportunity_id, NULL, kind, actor_id, occurred_at, data
FROM (
    SELECT l.id AS lead_id,
           NULL::uuid AS opportunity_id,
           a.action AS kind,
           u.id AS actor_id,
           a.occurred_at,
           0 AS source,
           a.id AS tiebreak,
           CASE a.action
               WHEN 'lead.created' THEN jsonb_build_object(
                   'status', a.metadata->>'status',
                   'status_name', s_to.name,
                   'owner_id', a.metadata->>'owner_id')
               WHEN 'lead.status_changed' THEN jsonb_build_object(
                   'from', a.metadata->>'from',
                   'from_name', s_from.name,
                   'to', a.metadata->>'to',
                   'to_name', s_to.name)
               WHEN 'lead.reassigned' THEN jsonb_build_object(
                   'from_owner_id', a.metadata->>'from_owner_id',
                   'to_owner_id', a.metadata->>'to_owner_id')
               ELSE '{}'::jsonb
           END AS data
    FROM audit_event a
    JOIN leads_lead l ON l.id::text = a.target_id
    LEFT JOIN identity_user u ON u.id = a.actor_id
    LEFT JOIN leads_lead_status s_from ON s_from.key = a.metadata->>'from'
    LEFT JOIN leads_lead_status s_to
        ON s_to.key = COALESCE(a.metadata->>'to', a.metadata->>'status')
    WHERE a.target_type = 'lead'
      AND a.action IN ('lead.created', 'lead.status_changed', 'lead.reassigned',
                       'lead.archived', 'lead.restored')
    UNION ALL
    SELECT o.lead_id,
           h.opportunity_id,
           CASE
               WHEN h.from_stage_id IS NULL THEN 'opportunity.created'
               WHEN h.from_status = 'open' AND h.to_status = 'won' THEN 'opportunity.won'
               WHEN h.from_status = 'open' AND h.to_status = 'lost' THEN 'opportunity.lost'
               WHEN h.from_status <> 'open' AND h.to_status = 'open' THEN 'opportunity.reopened'
               ELSE 'opportunity.stage_changed'
           END,
           h.actor_id,
           h.occurred_at,
           1,
           h.id,
           CASE
               WHEN h.from_stage_id IS NULL THEN jsonb_build_object(
                   'stage', h.to_stage_name,
                   'status', h.to_status,
                   'via_conversion', COALESCE(conversion.via_conversion, false))
               ELSE jsonb_build_object('from_stage', h.from_stage_name, 'to_stage', h.to_stage_name)
           END
    FROM pipeline_stage_history h
    JOIN pipeline_opportunity o ON o.id = h.opportunity_id
    LEFT JOIN LATERAL (
        SELECT bool_or(a.metadata->>'via' = 'conversion') AS via_conversion
        FROM audit_event a
        WHERE a.target_type = 'opportunity'
          AND a.action = 'opportunity.created'
          AND a.target_id = h.opportunity_id::text
    ) conversion ON h.from_stage_id IS NULL
) history
WHERE NOT EXISTS (SELECT 1 FROM activities_timeline_entry)
ORDER BY occurred_at, source, tiebreak;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("activities", "0002_integrity"),
        ("audit", "0002_audit_event_append_only"),
    ]

    operations = [
        # One INSERT ... SELECT over the whole history: at ~74,000 rows/s it would pass the
        # 10 s statement timeout around 740,000 history rows (Phase 4 review). The migration's
        # own transaction lifts it for itself only (SET LOCAL).
        migrations.RunSQL(
            sql=["SET LOCAL statement_timeout = 0;", BACKFILL],
            reverse_sql=migrations.RunSQL.noop,
        )
    ]
