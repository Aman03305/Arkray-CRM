"""Celery application.

Background work is driven by the transactional outbox (arkray.core.outbox): Celery is the
execution engine, PostgreSQL is the durable queue. See docs/reliability.md.
"""

import logging
import os
from typing import Any

from celery import Celery
from celery.signals import task_postrun, task_prerun, worker_init

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("arkray")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@worker_init.connect
def _check_database_role(**_: Any) -> None:
    """Workers refuse a privileged database role too (R74; beat runs no tasks). Celery logs
    and ignores an Exception from a signal handler (Phase 11: the worker started anyway), so
    the refusal is a SystemExit, which it doesn't catch."""
    from django.core.exceptions import ImproperlyConfigured

    from arkray.core.privileges import enforce_at_startup

    try:
        enforce_at_startup()
    except ImproperlyConfigured as exc:
        logging.getLogger("arkray.core.privileges").critical(
            "database_role_refused", extra={"reason": str(exc)}
        )
        raise SystemExit(1) from exc


@task_prerun.connect
def _bind_task_context(task_id: str, task: Any, **_: Any) -> None:
    from arkray.core.context import ExecutionContext, bind_context

    task.request.arkray_context_token = bind_context(
        ExecutionContext(correlation_id=task_id, task_id=task_id, task_name=task.name)
    )


@task_postrun.connect
def _reset_task_context(task: Any, **_: Any) -> None:
    from arkray.core.context import reset_context

    token = getattr(task.request, "arkray_context_token", None)
    if token is not None:
        reset_context(token)
