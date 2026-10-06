"""Attachment gauges for the metrics endpoint (docs/observability.md#attachment-storage).

- From PostgreSQL, at scrape time, in one query whose every arm reads the partial index the
  housekeeping uses (activities_attachment_open_idx): uploads in progress and the oldest's
  age, failed or deleted files whose object isn't removed yet and the oldest's age, files
  waiting for a malware scan. (Stored files in total are not counted here: that is a scan of
  the whole table every 30 s; the daily reconciliation reports the healthy count.)
- From the shared cache (activities.telemetry): what happened in the last hour without a row
  to show for it (uploads by outcome, failed downloads, lost objects, storage errors and
  latency) and the last reconciliation's outcome.
- `arkray_attachment_storage_up`: a liveness check of the store, at most every 30 s per
  process and within the probes' deadline.

Labels are operations, outcomes, reasons, error classes and the backend kind only: never a
file name, key, user or record.
"""

from __future__ import annotations

import logging
import math
import time

from django.db import connection

from arkray.core import metrics
from arkray.core.metrics import Gauge

from . import storage, telemetry

logger = logging.getLogger(__name__)


def gauges() -> list[Gauge]:
    """The database-backed gauges (a DatabaseError propagates: the endpoint handles it)."""
    rows = Gauge(
        "arkray_attachments",
        "Attachment rows by state: uploading, failed (object not yet removed), pending_scan.",
    )
    oldest = Gauge(
        "arkray_attachment_oldest_seconds",
        "Age of the oldest upload still in progress (state=uploading), and of the oldest failed"
        " or deleted file whose object isn't removed yet (state=unpurged); 0 when none.",
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "(SELECT 'uploading', count(*), EXTRACT(EPOCH FROM now() - min(created_at))"
            " FROM activities_attachment WHERE state = 'uploading')"
            " UNION ALL (SELECT 'unpurged', count(*), EXTRACT(EPOCH FROM now() - min(created_at))"
            " FROM activities_attachment WHERE purged_at IS NULL"
            " AND (deleted_at IS NOT NULL OR state = 'failed' OR scan_status = 'rejected'))"
            " UNION ALL (SELECT 'failed', count(*), NULL FROM activities_attachment"
            " WHERE purged_at IS NULL AND state = 'failed')"
            " UNION ALL (SELECT 'pending_scan', count(*), NULL FROM activities_attachment"
            " WHERE scan_status = 'pending' AND state = 'stored')"
        )
        found = {kind: (int(n), float(age or 0)) for kind, n, age in cursor.fetchall()}
    for kind in ("uploading", "failed", "pending_scan"):
        rows.add(found[kind][0], state=kind)
    for kind in ("uploading", "unpurged"):
        oldest.add(round(max(found[kind][1], 0.0), 1), state=kind)
    return [rows, oldest]


def storage_up() -> list[Gauge]:
    up = Gauge(
        "arkray_attachment_storage_up",
        "1 when the attachment store answered a liveness check (cached up to 30 s per process).",
    )
    try:
        backend = storage.backend_name()
        answered = storage.storage_up(metrics.PROBE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — a store that can't even be configured is down
        backend, answered = "other", False
    up.add(1 if answered else 0, backend=backend)
    return [up]


def events() -> list[Gauge]:
    """The cache-backed figures. Left out (not zero) when the cache can't answer."""
    try:
        totals = metrics.bounded(telemetry.last_hour)
        last = metrics.bounded(telemetry.last_reconcile)
    except Exception:  # noqa: BLE001 — unknown, not zero
        logger.warning("metrics_attachment_events_unknown")
        return []
    uploads = Gauge(
        "arkray_attachment_uploads_last_hour",
        "Uploads in the last hour by outcome (stored, failed) and reason (too_large, rejected,"
        " storage_error, timeout, quota, other). Shared by every process and pod.",
    )
    uploads.add(totals[("upload", "stored", "")], outcome="stored", reason="")
    for reason in telemetry.UPLOAD_REASONS:
        uploads.add(totals[("upload", "failed", reason)], outcome="failed", reason=reason)
    downloads = Gauge(
        "arkray_attachment_download_failures_last_hour",
        "Downloads that failed in the last hour: storage_unavailable (an outage, 503) or"
        " missing (the object is gone, 410).",
    )
    for reason in telemetry.DOWNLOAD_REASONS:
        downloads.add(totals[("download_failed", reason)], reason=reason)
    missing = Gauge(
        "arkray_attachment_objects_missing_last_hour",
        "Stored files whose object the store said was gone (downloads, scans), last hour.",
    )
    missing.add(totals[("object_missing",)])
    errors = Gauge(
        "arkray_attachment_storage_errors_last_hour",
        "Failed storage calls in the last hour by operation and error class (timeout,"
        " connection, server_error, access_denied, missing, busy, circuit_open, ...).",
    )
    latency = Gauge(
        "arkray_attachment_storage_latency_last_hour",
        "Storage calls in the last hour taking at most `le` seconds, by operation (cumulative"
        " buckets: histogram_quantile() works on them).",
    )
    for operation in telemetry.OPERATIONS:
        for error_class in telemetry.ERROR_CLASSES:
            count = totals[("error", operation, error_class)]
            if count:
                errors.add(count, operation=operation, error_class=error_class)
        running = 0
        for index, bound in enumerate(telemetry.LATENCY_BUCKETS):
            running += totals[("latency", operation, str(index))]
            le = "+Inf" if math.isinf(bound) else f"{bound:g}"
            latency.add(running, operation=operation, le=le)
    found = [uploads, downloads, missing, errors, latency]
    if last is not None:
        age = Gauge(
            "arkray_attachment_reconcile_age_seconds",
            "Seconds since the last whole reconciliation of attachments with storage ended.",
        )
        age.add(round(max(time.time() - float(last["finished_at"]), 0.0), 1))
        outcome = Gauge(
            "arkray_attachment_reconcile_outcome",
            "The last reconciliation's exit status: 0 healthy, 1 mismatches, 2 errors.",
        )
        outcome.add(int(last["outcome"]), mode=str(last.get("mode", "check")))
        mismatches = Gauge(
            "arkray_attachment_reconcile_mismatches",
            "What the last reconciliation found, by kind (missing, orphaned, size_mismatch,"
            " stale_pending, stale_failed, pending_scan, restorable, hash_mismatch).",
        )
        for kind, label in _RECONCILE_KINDS.items():
            mismatches.add(int(last.get(kind, 0)), kind=label)
        found += [age, outcome, mismatches]
    return found


# Reconciliation report keys -> the metric's `kind` labels.
_RECONCILE_KINDS = {
    "missing": "missing",
    "orphaned": "orphaned",
    "size_mismatch": "size_mismatch",
    "hash_mismatch": "hash_mismatch",
    "pending": "stale_pending",
    "failed": "stale_failed",
    "pending_scan": "pending_scan",
    "restorable": "restorable",
}
