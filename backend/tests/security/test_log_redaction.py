"""Logs never carry personal data, even when things fail (privacy remediation P2-2;
docs/observability.md#what-a-log-line-may-hold).

Planted values (a customer's name, email and phone, note text, a file name, a support reason,
an AI tool's arguments) go through real requests and tasks; each path is then made to fail
with an exception whose *message* quotes them, the way a database error or a careless
`raise ValueError(value)` would. Every line the logging configuration would write is
formatted with the production formatter and searched.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import logging
from pathlib import Path
from urllib.parse import quote

import pytest
from celery import shared_task
from django.db import IntegrityError
from django.utils import timezone

from arkray.ai import tools
from arkray.core import logging as arkray_logging
from arkray.core.access import AccessScope
from arkray.core.context import ExecutionContext, bind_context, reset_context
from arkray.core.logging import SAFE_FIELDS, ContextFilter, JsonFormatter
from arkray.identity.models import User
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

NAME = "Zorvana Quillfeather"
EMAIL = "zq.canary@leak.example"
PHONE = "+91 98111 22334"
NOTE = "PLANTED-NOTE-TEXT about Zorvana's HbA1c"
FILE = "Zorvana_Aadhaar_card.pdf"
REASON = "Zorvana on medical leave"
CANARIES = {NAME, "Zorvana", EMAIL, "zq.canary", PHONE, "98111", NOTE, "HbA1c", FILE, REASON}
BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture
def captured(caplog):
    caplog.set_level(logging.DEBUG)

    def text() -> str:
        formatter = JsonFormatter()
        lines = []
        for record in caplog.records:
            ContextFilter().filter(record)
            lines.append(formatter.format(record))
        return "\n".join(lines)

    return text


def leaked(text: str) -> set[str]:
    return {canary for canary in CANARIES if canary in text}


def failing_client(user):
    client = signed_in(user)
    client.raise_request_exception = False  # the 500 path, logged by django.request
    return client


# --- failures on every module's write path ----------------------------------------------------
def test_pipeline_failure_quoting_the_customer(user_a, captured, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError(f"customer {NAME} <{EMAIL}> {PHONE}")

    monkeypatch.setattr("arkray.pipeline.services._mark_converted", boom)
    response = failing_client(user_a).post(
        "/api/v1/workspaces/me/opportunities",
        {
            "value": "1200000",
            "customer_name": NAME,
            "account_name": "Zorvana Labs",
            "contact_email": EMAIL,
            "contact_phone": PHONE,
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="11111111-2222-4333-8444-555555555555",
    )
    assert response.status_code == 500
    logged = captured()
    assert "ValueError" in logged  # the type and the frames are there
    assert "_mark_converted" in logged or "boom" in logged
    assert not leaked(logged)


def test_note_failure_quoting_the_note(user_a, captured, monkeypatch):
    lead = LeadFactory(owner=user_a, created_by=user_a)
    opportunity = OpportunityFactory(lead=lead)

    def boom(*args, **kwargs):
        raise RuntimeError(NOTE)

    monkeypatch.setattr("arkray.activities.services._publish", boom)
    response = failing_client(user_a).post(
        "/api/v1/workspaces/me/activities",
        {"type": "note", "description": NOTE, "opportunity": str(opportunity.pk)},
        format="json",
    )
    assert response.status_code == 500
    logged = captured()
    assert "RuntimeError" in logged
    assert not leaked(logged)


def test_admin_user_management_failure_quoting_the_email(admin, captured, monkeypatch):
    def boom(user_id):
        raise KeyError(f"{NAME} {EMAIL}")

    monkeypatch.setattr("arkray.identity.services.selectors.admin_user_detail", boom)
    response = failing_client(admin).post(
        "/api/v1/admin/users",
        {
            "email": EMAIL,
            "first_name": "Zorvana",
            "last_name": "Quillfeather",
            "role": "sales_user",
        },
        format="json",
    )
    assert response.status_code == 500
    logged = captured()
    assert "KeyError" in logged
    assert not leaked(logged)


def test_attachment_failure_quoting_the_file_name(user_a, captured, monkeypatch):
    lead = LeadFactory(owner=user_a, created_by=user_a)
    note = NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)

    def boom(*args, **kwargs):
        raise OSError(f"cannot store {FILE}")

    monkeypatch.setattr("arkray.activities.attachments._finish", boom)
    response = failing_client(user_a).post(
        f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
        b"%PDF-1.7\n%%EOF\n",
        content_type="application/octet-stream",
        HTTP_X_FILENAME=quote(FILE, safe=""),
    )
    assert response.status_code >= 500
    logged = captured()
    assert not leaked(logged)


def test_support_reason_is_never_logged(admin, user_a, captured):
    response = signed_in(admin).post(
        "/api/v1/admin/support-sessions", {"user": str(user_a.pk), "reason": REASON}, format="json"
    )
    assert response.status_code in (200, 201), response.content
    assert not leaked(captured())


def test_ai_tool_failure_is_logged_by_type_only(user_a, captured, monkeypatch):
    ctx = tools.ToolContext(scope=AccessScope.own(user_a.pk), now=timezone.now())
    name = tools.available(ctx.scope)[0].name

    def boom(ctx, arguments):
        raise ValueError(f"{arguments}")

    failing = dataclasses.replace(tools._BY_NAME[name], run=boom)
    monkeypatch.setitem(tools._BY_NAME, name, failing)
    monkeypatch.setattr(tools, "available", lambda scope: [failing])
    outcome = tools.execute(ctx, name, {"query": NAME, "email": EMAIL})
    assert outcome.is_error
    logged = captured()
    assert "ai_tool_failed" in logged
    assert '"exc_type": "ValueError"' in logged
    assert not leaked(logged)


@shared_task(name="tests.duplicate_user")
def _duplicate_user() -> None:
    # Names, not values, on the source line: a traceback shows the code of every frame.
    first, last = NAME.split()
    User.objects.create(email=EMAIL, first_name=first, last_name=last)


def test_celery_task_failure_never_logs_the_failing_rows_values(captured):
    """Celery logs a failure with the exception's repr in its message and the traceback in
    an extra field called `data`; PostgreSQL's message quotes the row ("Key (email)=...")."""
    User.objects.create(email=EMAIL, first_name="Z", last_name="Q")
    result = _duplicate_user.apply()
    assert isinstance(result.result, IntegrityError)
    logged = captured()
    assert "tests.duplicate_user" in logged  # the line is there, with its task name
    assert "raised unexpected" in logged
    assert "IntegrityError" in logged
    assert not leaked(logged)
    assert '"data":' not in logged  # the field itself; its name may be listed as withheld


# --- the formatter's rules ----------------------------------------------------------------------
def _record(msg="event", name="arkray.test", args=(), exc_info=None, **extra):
    record = logging.LogRecord(name, logging.ERROR, __file__, 1, msg, args, exc_info)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_unknown_fields_are_withheld_by_name():
    line = json.loads(JsonFormatter().format(_record(customer=NAME, question_id="q-1")))
    assert line["question_id"] == "q-1"
    assert "customer" not in line
    assert line["withheld"] == ["customer"]
    assert NAME not in json.dumps(line)


def test_exception_messages_are_withheld_but_frames_kept():
    try:
        int(f"{PHONE}")
    except ValueError:
        import sys

        line = json.loads(JsonFormatter().format(_record(exc_info=sys.exc_info())))
    assert line["exc_type"] == "ValueError"
    assert "test_exception_messages_are_withheld_but_frames_kept" in line["exc"]
    assert "[message withheld]" in line["exc"]
    assert PHONE not in line["exc"]


def test_exception_messages_can_be_shown_in_local_development(settings):
    settings.LOG_EXCEPTION_MESSAGES = True
    try:
        raise ValueError("visible locally")
    except ValueError:
        import sys

        text = arkray_logging.format_exception(sys.exc_info())
    assert "visible locally" in text


def test_chained_exceptions_withhold_every_message():
    try:
        try:
            raise KeyError(EMAIL)
        except KeyError as inner:
            raise RuntimeError(NAME) from inner
    except RuntimeError:
        import sys

        text = arkray_logging.format_exception(sys.exc_info())
    assert "KeyError" in text
    assert "RuntimeError" in text
    assert "direct cause" in text
    assert not leaked(text)


def test_third_party_messages_lose_free_text_arguments():
    celery_line = _record(
        "Task %(name)s[%(id)s] %(description)s: %(exc)s",
        name="celery.app.trace",
        args=({"name": "x.y", "id": "abc-1", "description": "raised unexpected", "exc": EMAIL},),
    )
    text = JsonFormatter().format(celery_line)
    assert "x.y[abc-1]" in text
    assert EMAIL not in text
    gunicorn_line = _record(
        "Error handling request %s", name="gunicorn.error", args=("/api/v1/search?q=Zorvana",)
    )
    text = JsonFormatter().format(gunicorn_line)
    assert "/api/v1/search" in text
    assert "Zorvana" not in text
    formatted = _record(
        "Invalid request from ip=1.2.3.4: Invalid HTTP request line: 'GET /x?q=Zorvana HTTP/1.1'",
        name="gunicorn.error",
    )
    text = JsonFormatter().format(formatted)
    assert "Zorvana" not in text
    assert "1.2.3.4" not in text  # backend review P3: the address on the access line only
    third_party = _record("Connected to %s", name="django_extensions.x", args=(EMAIL,))
    assert EMAIL not in JsonFormatter().format(third_party)  # not Django's own logger


def test_the_client_address_is_on_the_access_line_only(user_a_client, caplog):
    caplog.set_level(logging.INFO)
    user_a_client.get("/api/v1/auth/me", REMOTE_ADDR="203.0.113.9")
    token = bind_context(ExecutionContext(correlation_id="r-1", client_ip="203.0.113.9"))
    try:
        other = _record("something_happened")
        ContextFilter().filter(other)
    finally:
        reset_context(token)
    assert "client_ip" not in json.loads(JsonFormatter().format(other))
    access = [r for r in caplog.records if r.getMessage() == "http_request"]
    assert access
    assert json.loads(JsonFormatter().format(access[-1]))["client_ip"] == "203.0.113.9"


def test_values_of_allowed_fields_are_bounded():
    line = json.loads(JsonFormatter().format(_record(reason="x" * 5000, tools=[object()])))
    assert len(line["reason"]) <= 501
    assert line["tools"] == ["[object]"]


def _logged_field_names() -> set[str]:
    """Every literal `extra=` key in the application code."""
    names: set[str] = set()
    for path in (BACKEND / "arkray").rglob("*.py"):
        if "tests" in path.parts or "migrations" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                for keyword in node.keywords:
                    if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                        names |= {
                            k.value
                            for k in keyword.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)
                        }
    return names


def test_every_field_the_code_logs_is_deliberately_allowed():
    from arkray.activities import reconcile

    dynamic = {
        "attempt",  # core.outbox's log fields
        *reconcile.Report(mode="check").counts(),  # activities' reconcile report
        "records",  # ai.service's answer metadata
    }
    missing = (_logged_field_names() | dynamic) - SAFE_FIELDS
    assert not missing, f"add to core.logging.SAFE_FIELDS (if safe): {sorted(missing)}"
