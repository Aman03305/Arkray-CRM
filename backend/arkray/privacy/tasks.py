"""Privacy housekeeping (Celery beat, hourly): delete the files of expired data exports
(privacy.exports). Safe to run late or twice."""

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
    return {"expired_exports": expired}
