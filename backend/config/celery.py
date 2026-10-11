"""Celery application.

Background work is driven by the transactional outbox (arkray.core.outbox): Celery is the
execution engine, PostgreSQL is the durable queue. See docs/reliability.md.
"""

import logging
import os
import threading
import time
from typing import Any

from celery import Celery, Task
from celery.signals import task_postrun, task_prerun, worker_init

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

gate_logger = logging.getLogger("arkray.core.ledger_gate")

# A refused task is logged at ERROR at most this often per event and task name in a process
# (the relay alone ticks every 5 s); the line counts the ones left out in between.
GATE_LOG_INTERVAL_S = 60.0
_gate_log_lock = threading.Lock()
_gate_logged: dict[tuple[str, str], tuple[float, int]] = {}


def _log_gate_refusal(event: str, task: Task, status: str, **fields: Any) -> None:  # type: ignore[type-arg]
    key, now = (event, task.name), time.monotonic()
    with _gate_log_lock:
        logged_at, suppressed = _gate_logged.get(key, (None, 0))
        if logged_at is not None and now - logged_at < GATE_LOG_INTERVAL_S:
            _gate_logged[key] = (logged_at, suppressed + 1)
            return
        _gate_logged[key] = (now, 0)
    # `count`: the refusals of this kind left out since the last line (allowlisted fields
    # only, core.logging).
    gate_logger.error(
        event, extra={"task_name": task.name, "status": status, "count": suppressed, **fields}
    )


def reset_gate_log() -> None:
    """Forget the rate limiter's state (tests)."""
    with _gate_log_lock:
        _gate_logged.clear()


class GatedTask(Task):  # type: ignore[type-arg]
    """Every task waits while the database doesn't match the erasure ledger
    (core.ledger_gate), as the API does: a restored backup's queued work (an export of
    someone erased since, an invitation to a pseudonymised address, re-indexing erased text)
    never runs before `replay_erasures` has re-applied the erasures (backend review P2).

    Fail closed, never silently (final audit ARCH-1: workers without the ledger's volume
    skipped every task with a WARNING, and Ask Arkray questions sent straight to the ai
    queue were lost):

    - a periodic task (a beat entry: the relay, housekeeping, reconciliations) is skipped:
      its next tick runs it. The relay is one, so no outbox event is claimed meanwhile;
    - any other task (an outbox event's message, an Ask Arkray question) is sent back to its
      queue with a growing delay (60 s, doubling, at most 5 minutes), where a worker whose
      gate is open may take it, at most `ledger_gate_max_retries` times (about 50 minutes);
      after that it is dropped. An outbox event's lease then returns it to pending (nothing
      is lost); a question has expired long before (90 s) and fails `timeout`;
    - each refusal logs at ERROR (`background_task_skipped`, `background_task_deferred`,
      `background_task_dropped`, with the task's name and the gate's status), at most once a
      minute per task name and process; the container's health check
      (`manage.py ledger_status`) turns unhealthy too.

    `ledger_gate_exempt = True` (a task option) lets a task run while the gate is closed:
    only for work that repairs the ledger's state and touches nothing the gate protects."""

    ledger_gate_exempt = False
    ledger_gate_retry_delay_s = 60
    ledger_gate_retry_max_delay_s = 300
    ledger_gate_max_retries = 12

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        from arkray.core import ledger_gate

        if self.ledger_gate_exempt:
            return super().__call__(*args, **kwargs)
        if ledger_gate.is_open():
            return super().__call__(*args, **kwargs)
        return self._refuse(ledger_gate.GATE.status())  # cached: only for the log line

    def _is_periodic(self) -> bool:
        schedule = self.app.conf.beat_schedule or {}
        return any(entry.get("task") == self.name for entry in schedule.values())

    def _refuse(self, status: str) -> None:
        request, attempt = self.request, self.request.retries or 0
        if self._is_periodic():
            _log_gate_refusal("background_task_skipped", self, status)
            return None
        if request.called_directly or attempt >= self.ledger_gate_max_retries:
            # Called as a function (no queue to go back to), or out of retries.
            _log_gate_refusal("background_task_dropped", self, status, attempt=attempt)
            return None
        delay = min(self.ledger_gate_retry_delay_s * 2**attempt, self.ledger_gate_retry_max_delay_s)
        _log_gate_refusal(
            "background_task_deferred", self, status, attempt=attempt + 1, retry_in_s=delay
        )
        # Raises celery's Retry: the message is acknowledged once its copy is queued.
        raise self.retry(countdown=delay, max_retries=self.ledger_gate_max_retries)


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
