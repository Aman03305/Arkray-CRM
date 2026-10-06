"""Password secrecy (docs/security.md#password-secrecy): nobody, administrators included, can
ever retrieve a user's password (old, new or temporary), its hash, or a one-time
reset/invitation secret.

Every password flow runs once with unique canary strings: an administrator creates a user with
an initial password and later sets a temporary one, the user changes theirs (twice, at the
forced changes), a sign-in fails, a reset is requested, mailed and confirmed, an invitation
is accepted, and an unexpected error strikes in the middle of the set-password request. Then
every place a secret could surface is searched: API bodies and headers, the audit table, the
outbox, the log output at DEBUG, every email, and every text and JSON column of every table in
the database. The administrator knows the temporary password they typed (ADR-0026: the user
must replace it at first sign-in); no API returns it, and nothing stores it in the clear."""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core import mail
from django.db import connection
from rest_framework import serializers
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.core.api import StrictInputSerializer
from arkray.core.logging import JsonFormatter
from arkray.core.models import OutboxEvent
from arkray.identity.models import AccountToken, User
from tests.helpers import drain_outbox, emailed_secrets, signed_in

pytestmark = pytest.mark.django_db

RAHUL = "rahul.secrecy@example.test"
NEHA = "neha.secrecy@example.test"
LOGGERS = ("", "arkray", "django", "django.request", "django.security", "celery")


def canary(label: str) -> str:
    return f"Cnry-{label}-{secrets.token_hex(6)}!"


@dataclass
class Run:
    passwords: dict[str, str]
    responses: list[Any] = field(default_factory=list)
    hashes: set[str] = field(default_factory=set)
    links: dict[str, str] = field(default_factory=dict)  # raw one-time secret -> its owner

    def keep(self, response: Any) -> Any:
        self.responses.append(response)
        return response

    def note_hashes(self) -> None:
        self.hashes.update(
            h
            for h in User.objects.filter(email__in=[RAHUL, NEHA]).values_list("password", flat=True)
        )

    @property
    def plaintexts(self) -> set[str]:
        return set(self.passwords.values()) | set(self.links)

    @property
    def everything(self) -> set[str]:
        return self.plaintexts | self.hashes


def run_every_password_flow(admin: User, monkeypatch: pytest.MonkeyPatch) -> Run:
    run = Run(
        passwords={
            label: canary(label)
            for label in (
                "initial",
                "chosen",
                "temporary",
                "chosen2",
                "wrong",
                "unknown",
                "reset",
                "invited",
                "crash",
            )
        }
    )
    pw = run.passwords
    admin_client = signed_in(admin)

    # An administrator creates Rahul with an initial password; Rahul replaces it.
    created = run.keep(
        admin_client.post(
            "/api/v1/admin/users",
            {
                "first_name": "Rahul",
                "email": RAHUL,
                "role": "sales_user",
                "password": pw["initial"],
            },
            format="json",
        )
    )
    assert created.status_code == 201, created.content
    run.note_hashes()
    rahul = APIClient()
    assert run.keep(sign_in(rahul, RAHUL, pw["initial"])).status_code == 200
    assert run.keep(change(rahul, pw["initial"], pw["chosen"])).status_code == 204
    run.note_hashes()

    # The administrator sets a temporary password; Rahul signs in with it and replaces it.
    version = User.objects.get(email=RAHUL).version
    temporary = run.keep(
        admin_client.post(
            f"/api/v1/admin/users/{created.json()['id']}/set-password",
            {"version": version, "new_password": pw["temporary"]},
            format="json",
        )
    )
    assert temporary.status_code == 200, temporary.content
    run.note_hashes()
    rahul = APIClient()
    assert run.keep(sign_in(rahul, RAHUL, pw["temporary"])).status_code == 200
    assert run.keep(change(rahul, pw["temporary"], pw["chosen2"])).status_code == 204
    run.note_hashes()

    # Failed sign-ins: a wrong password, an unknown account.
    assert run.keep(sign_in(APIClient(), RAHUL, pw["wrong"])).status_code == 400
    assert run.keep(sign_in(APIClient(), "nobody@example.test", pw["unknown"])).status_code == 400

    # Forgot password: request, email, confirm.
    public = APIClient()
    run.keep(public.post("/api/v1/auth/password-reset", {"email": RAHUL}, format="json"))
    drain_outbox()
    (reset,) = emailed_secrets("reset-password")
    run.links[reset] = RAHUL
    confirmed = run.keep(
        public.post(
            "/api/v1/auth/password-reset/confirm",
            {"token": reset, "new_password": pw["reset"]},
            format="json",
        )
    )
    assert confirmed.status_code == 204, confirmed.content
    run.note_hashes()

    # An invitation, accepted.
    invited = run.keep(
        admin_client.post(
            "/api/v1/admin/users",
            {"first_name": "Neha", "email": NEHA, "role": "sales_user"},
            format="json",
        )
    )
    drain_outbox()
    (invitation,) = emailed_secrets("activate")
    run.links[invitation] = NEHA
    run.keep(public.post("/api/v1/auth/invitations/verify", {"token": invitation}, format="json"))
    accepted = run.keep(
        public.post(
            "/api/v1/auth/invitations/accept",
            {"token": invitation, "password": pw["invited"]},
            format="json",
        )
    )
    assert accepted.status_code == 200, accepted.content
    run.note_hashes()

    # An unexpected failure while the administrator's typed password is in the request.
    def unavailable(self: User, raw_password: str | None) -> None:
        raise RuntimeError("password hasher unavailable")

    with monkeypatch.context() as patched:
        patched.setattr(User, "set_password", unavailable)
        admin_client.raise_request_exception = False
        neha_id = invited.json()["id"]
        crashed = run.keep(
            admin_client.post(
                f"/api/v1/admin/users/{neha_id}/set-password",
                {"version": User.objects.get(pk=neha_id).version, "new_password": pw["crash"]},
                format="json",
            )
        )
        assert crashed.status_code == 500

    # Everything an administrator or the user can read about the accounts afterwards.
    neha = APIClient()
    assert run.keep(sign_in(neha, NEHA, pw["invited"])).status_code == 200
    rahul = APIClient()
    assert run.keep(sign_in(rahul, RAHUL, pw["reset"])).status_code == 200
    rahul_id = created.json()["id"]
    for path in (
        "/api/v1/admin/users",
        f"/api/v1/admin/users/{rahul_id}",
        f"/api/v1/admin/users/{neha_id}",
        "/api/v1/admin/users?q=Rahul",
        "/api/v1/admin/security-events",
        "/api/v1/auth/me",
        "/api/v1/workspaces/me",
        f"/api/v1/workspaces/{rahul_id}",
        "/api/v1/workspaces/all/search?q=Rahul",
        "/api/v1/workspaces/all/ask",
        "/api/v1/assignees",
    ):
        run.keep(admin_client.get(path))
    for client in (rahul, neha):
        for path in (
            "/api/v1/auth/me",
            "/api/v1/workspaces/me",
            "/api/v1/workspaces/me/search?q=a",
        ):
            run.keep(client.get(path))
    return run


def sign_in(client: APIClient, email: str, password: str) -> Any:
    return client.post("/api/v1/auth/login", {"email": email, "password": password}, format="json")


def change(client: APIClient, current: str, new: str) -> Any:
    return client.post(
        "/api/v1/auth/password/change",
        {"current_password": current, "new_password": new},
        format="json",
    )


def leaks(needles: set[str], haystack: str) -> set[str]:
    return {needle for needle in needles if needle in haystack}


@pytest.fixture
def flows(admin, caplog, monkeypatch):
    for name in LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)
    run = run_every_password_flow(admin, monkeypatch)
    return run, caplog.records


def test_the_flows_left_real_hashes_and_links_to_look_for(flows):
    run, _ = flows
    assert len(run.hashes) >= 6  # one per password set
    assert all(h.startswith(("md5$", "argon2", "pbkdf2")) for h in run.hashes)
    assert len(run.links) == 2


def test_no_api_response_body_or_header_carries_a_secret(flows):
    run, _ = flows
    digests = set(
        AccountToken.objects.exclude(token_hash=None).values_list("token_hash", flat=True)
    )
    for response in run.responses:
        where = (response.request["REQUEST_METHOD"], response.request["PATH_INFO"])
        headers = "\n".join(f"{k}: {v}" for k, v in response.items())
        body = response.content.decode()
        assert not leaks(run.everything | digests, body + headers), where
        for name in ('"password"', '"new_password"', '"token_hash"', '"token"', '"session_epoch"'):
            assert name not in body, (where, name)


def test_the_audit_table_holds_no_secret(flows):
    run, _ = flows
    audited = json.dumps(list(AuditEvent.objects.values()), default=str)
    assert AuditEvent.objects.filter(action="auth.password_set_by_admin").exists()
    assert not leaks(run.everything, audited)


def test_the_outbox_holds_no_secret(flows):
    """The reset request's payload holds the *address* until housekeeping blanks it (SEC-8,
    accepted); never a password, a hash or a link secret."""
    run, _ = flows
    queued = json.dumps(list(OutboxEvent.objects.values()), default=str)
    assert not leaks(run.everything, queued)


def test_the_log_output_holds_no_secret_even_at_debug(flows):
    run, records = flows
    logged = "\n".join(JsonFormatter().format(record) for record in records)
    assert any(r.exc_info for r in records), "the crash was logged with its traceback"
    assert not leaks(run.everything, logged)


def test_each_link_goes_only_to_its_own_account_and_no_email_carries_a_password(flows):
    run, _ = flows
    for message in mail.outbox:
        text = f"{message.subject}\n{message.body}"
        assert not leaks(set(run.passwords.values()) | run.hashes, text)
        assert len(message.to) == 1
        assert not message.cc
        assert not message.bcc
        for link, owner in run.links.items():
            if link in text:
                assert message.to == [owner]
    assert sorted(m.to[0] for m in mail.outbox) == sorted(run.links.values())


def text_columns() -> list[tuple[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.table_name, c.column_name
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE'
              AND c.data_type IN ('text', 'character varying', 'character', 'json', 'jsonb')
            ORDER BY 1, 2
            """
        )
        return list(cursor.fetchall())


def where_found(needle: str, columns: list[tuple[str, str]]) -> list[str]:
    found = []
    with connection.cursor() as cursor:
        for table, column in columns:
            cursor.execute(
                f'SELECT EXISTS (SELECT 1 FROM "{table}" WHERE strpos("{column}"::text, %s) > 0)',  # noqa: S608 — names from the catalogue
                [needle],
            )
            if cursor.fetchone()[0]:
                found.append(f"{table}.{column}")
    return found


def test_no_table_stores_a_password_or_link_in_the_clear_nor_copies_a_hash(flows):
    """Strongest check: every text and JSON column of every table in the database."""
    run, _ = flows
    columns = text_columns()
    assert len(columns) > 50  # really every table, not a filtered few
    for plaintext in run.plaintexts:
        assert where_found(plaintext, columns) == [], plaintext
    password_column = f"{User._meta.db_table}.password"
    current = set(User.objects.values_list("password", flat=True))
    for hashed in run.hashes:
        expected = [password_column] if hashed in current else []
        assert where_found(hashed, columns) == expected
    # Session rows are encoded: search what they decode to as well.
    sessions = json.dumps(
        [SessionStore().decode(s.session_data) for s in Session.objects.all()], default=str
    )
    assert Session.objects.exists()
    assert not leaks(run.everything, sessions)


# --- serializers: the exact fields every user-returning serializer exposes --------------------
USER_SERIALIZER_FIELDS = {
    "arkray.identity.api.serializers.ViewerSerializer": {
        "id",
        "email",
        "first_name",
        "last_name",
        "full_name",
        "role",
        "role_label",
        "capabilities",
        "features",
        "password_change_required",
        "support_session",
    },
    "arkray.identity.api.serializers.AdminUserSerializer": {
        "id",
        "email",
        "first_name",
        "last_name",
        "full_name",
        "role",
        "role_label",
        "status",
        "status_label",
        "last_login",
        "created_at",
        "activated_at",
        "deactivated_at",
        "invitation",
        "password_change_required",
        "password_changed_at",
        "version",
    },
    "arkray.identity.api.serializers.WorkspaceSubjectSerializer": {"id", "full_name", "status"},
    "arkray.identity.api.serializers.AssigneeSerializer": {"id", "full_name", "email"},
    "arkray.leads.api.serializers.UserRefSerializer": {"id", "full_name", "is_active"},
}
SERIALIZER_MODULES = [
    "arkray.identity.api.serializers",
    "arkray.leads.api.serializers",
    "arkray.pipeline.api.serializers",
    "arkray.activities.api.serializers",
    "arkray.dashboard.api.serializers",
    "arkray.search.api.serializers",
    "arkray.ai.api.serializers",
]
NEVER_OUTPUT = {
    "password",
    "new_password",
    "current_password",
    "token",
    "token_hash",
    "session_epoch",
    "is_superuser",
}


def all_serializers() -> list[type[serializers.Serializer[Any]]]:
    import importlib
    import inspect

    found = []
    for name in SERIALIZER_MODULES:
        module = importlib.import_module(name)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, serializers.Serializer) and cls.__module__ == name:
                found.append(cls)
    return found


def test_every_user_serializer_exposes_exactly_its_pinned_fields():
    """A new serializer of User, or a new field on one, fails here until reviewed."""
    seen = {}
    for cls in all_serializers():
        meta = getattr(cls, "Meta", None)
        if getattr(meta, "model", None) is User:
            seen[f"{cls.__module__}.{cls.__name__}"] = set(cls().fields)
    assert seen == USER_SERIALIZER_FIELDS


def test_no_output_serializer_declares_a_secret_field():
    """Input serializers take passwords and tokens; nothing that renders a response does."""
    for cls in all_serializers():
        if issubclass(cls, StrictInputSerializer):
            continue
        if issubclass(cls, serializers.ModelSerializer) and not hasattr(cls, "Meta"):
            names = set(cls._declared_fields)  # an abstract base (its subclasses are checked)
        else:
            names = set(cls().fields)
        assert not names & NEVER_OUTPUT, (cls, names & NEVER_OUTPUT)
