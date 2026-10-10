"""Celery application.

Background work is driven by the transactional outbox (arkray.core.outbox): Celery is the
execution engine, PostgreSQL is the durable queue. See docs/reliability.md.
"""

import logging
import os
from typing import Any

from celery import Celery, Task
from celery.signals import task_postrun, task_prerun, worker_init

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")


class GatedTask(Task):  # type: ignore[type-arg]
    """Every task waits while the database doesn't match the erasure ledger
    (core.ledger_gate), as the API does: a restored backup's queued work (an export of
    someone erased since, an invitation to a pseudonymised address, re-indexing erased text)
    never runs before `replay_erasures` has re-applied the erasures (backend review P2).
    The outbox relay is a task too, so nothing is claimed meanwhile; an outbox message
    already sent is skipped before it counts an attempt, and its lease returns it to pending
    once the gate reopens."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        from arkray.core import ledger_gate

        if not ledger_gate.is_open():
            logging.getLogger("arkray.core.ledger_gate").warning(
                "background_task_deferred", extra={"status": ledger_gate.GATE.status()}
            )
            return None
        return super().__call__(*args, **kwargs)


app = Celery("arkray", task_cls=GatedTask)
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
