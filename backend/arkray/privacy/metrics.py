"""Data-export file gauges for the metrics endpoint (privacy.exports; final audit PRIV-3;
docs/runbooks.md#export-file-cleanup).

- From PostgreSQL, at scrape time (the table is small: a few exports a day): ended exports
  whose file isn't confirmed deleted yet (pending), those the store's retention refused
  (retained), those failed PURGE_ALERT_AFTER times or more (stuck), and the oldest's age.
The last reconciliation's outcomes are in its log lines (`export_orphans_reconciled`,
`export_reconcile_refused`, `export_reconcile_failed`), not here: a cache read would be one
more bounded probe in every scrape, which a stalled cache makes slow (the scrape must answer
during that outage).

Labels are states and outcomes only: never a key, an export or a person.
"""

from __future__ import annotations

from django.db import connection

from arkray.core.metrics import Gauge

from . import exports


def gauges() -> list[Gauge]:
    """A DatabaseError propagates: the endpoint handles it."""
    purges = Gauge(
        "arkray_export_file_purges",
        "Ended data exports whose file is not confirmed deleted, by state: pending (all of"
        " them), retained (refused by the store's retention or legal hold, or a missing"
        " permission), stuck (failed repeatedly; see docs/runbooks.md#export-file-cleanup).",
    )
    oldest = Gauge(
        "arkray_export_file_purge_oldest_seconds",
        "Seconds since the oldest still-pending export file deletion was requested (0: none).",
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*), count(*) FILTER (WHERE purge_error = %s),"
            " count(*) FILTER (WHERE purge_attempts >= %s),"
            " EXTRACT(EPOCH FROM now() - min(purge_requested_at))"
            " FROM privacy_data_export"
            " WHERE purge_requested_at IS NOT NULL AND purged_at IS NULL",
            [exports.RETAINED, exports.PURGE_ALERT_AFTER],
        )
        pending, retained, stuck, age = cursor.fetchone() or (0, 0, 0, None)
    purges.add(int(pending), state="pending")
    purges.add(int(retained), state="retained")
    purges.add(int(stuck), state="stuck")
    oldest.add(round(max(float(age or 0), 0.0), 1))
    return [purges, oldest]
