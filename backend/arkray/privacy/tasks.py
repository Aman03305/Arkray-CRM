"""Privacy housekeeping (Celery beat, hourly): end expired data exports and delete the files
of ended ones, retrying those whose deletion failed, then reconcile the exports store
(privacy.exports; docs/runbooks.md#export-file-cleanup). Safe to run late or twice."""

from __future__ import annotations

import logging

from celery import shared_task

from . import exports

logger = logging.getLogger(__name__)


@shared_task(name="privacy.housekeeping", ignore_result=True)
def housekeeping() -> dict[str, int]:
    expired = exports.expire()
    if expired:
        logger.info("privacy_housekeeping", extra={"count": expired})
    exports.reconcile_orphans()  # logs what it found; the metrics show its last run
    return {"expired_exports": expired}
