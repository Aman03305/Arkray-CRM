"""Celery application.

Background work is driven by the transactional outbox (arkray.core.outbox): Celery is the
execution engine, PostgreSQL is the durable queue. See docs/reliability.md.
"""

import os
from typing import Any

from celery import Celery
from celery.signals import task_postrun, task_prerun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("arkray")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


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
