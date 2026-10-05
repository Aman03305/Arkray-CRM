"""Workspace resolution: the Admin -> User Workspace mechanism.

Every CRM API route is nested under `/api/v1/workspaces/{workspace}/...` where
`{workspace}` is:

    "me"      -> the actor's own records                       (AccessScope SELF)
    "<uuid>"  -> that user's records; needs WORKSPACE_VIEW_ANY  (AccessScope USER, audited)
    "all"     -> every user's records; needs CRM_VIEW_ALL       (AccessScope ORGANIZATION, audited)

This is *authorised data access*, not impersonation: the admin stays signed in as
themselves, every write records the admin as the actor, and no session or credential of the
viewed user is ever touched. Anything the actor may not open yields NotFoundError (404), so
the endpoint cannot be used to discover which user IDs exist.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core.access import AccessScope, ScopeKind
from arkray.core.context import current_support_target_id, update_context
from arkray.core.errors import NotFoundError, PermissionDeniedError, SupportSessionActive

from .models import User, WorkspaceAccessWindow
from .policy import Capability, has_capability

logger = logging.getLogger(__name__)

WORKSPACE_SELF = "me"
WORKSPACE_ORGANIZATION = "all"
AUDIT_ACTION_WORKSPACE_ACCESSED = "workspace.accessed"
# Only the canonical hyphenated form is a workspace reference ("urn:uuid:", braces, bare hex
# and other spellings Python's UUID() accepts are rejected), so every URL is canonical.
_CANONICAL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class _Actor(Protocol):
    @property
    def pk(self) -> UUID: ...

    @property
    def is_authenticated(self) -> bool: ...

    @property
    def is_active(self) -> bool: ...


SUPPORT_ELSEWHERE = (
    "You're in a support session for another user's CRM. Exit it to open other workspaces."
)


def resolve_workspace(actor: _Actor, workspace: str) -> AccessScope:
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDeniedError()
    # In a support session (identity.support) the only workspace is the target user's.
    support_target = current_support_target_id()
    if support_target is not None and not (
        _CANONICAL_UUID.fullmatch(workspace) and UUID(workspace) == support_target
    ):
        raise SupportSessionActive(SUPPORT_ELSEWHERE)

    if workspace == WORKSPACE_SELF:
        if not has_capability(actor, Capability.CRM_ACCESS_OWN):
            raise PermissionDeniedError()
        return AccessScope.own(actor.pk)

    if workspace == WORKSPACE_ORGANIZATION:
        if not has_capability(actor, Capability.CRM_VIEW_ALL):
            raise NotFoundError()
        scope = AccessScope.organization(actor.pk)
        _record_delegated_access(scope)
        return scope

    if not _CANONICAL_UUID.fullmatch(workspace):
        raise NotFoundError()
    subject_id = UUID(workspace)
    if subject_id == actor.pk:
        return resolve_workspace(actor, WORKSPACE_SELF)
    if not has_capability(actor, Capability.WORKSPACE_VIEW_ANY):
        raise NotFoundError()
    # Deactivated users stay viewable: their history is preserved, not hidden.
    if not User.objects.filter(pk=subject_id).exists():
        raise NotFoundError()
    scope = AccessScope.for_user(actor.pk, subject_id)
    _record_delegated_access(scope)
    return scope


def workspace_segment(scope: AccessScope) -> str:
    """The `{workspace}` URL segment that addresses `scope` ("me", "all" or a user id)."""
    if scope.kind is ScopeKind.SELF:
        return WORKSPACE_SELF
    if scope.kind is ScopeKind.ORGANIZATION:
        return WORKSPACE_ORGANIZATION
    return str(scope.subject_user_id)


def authorize_write(actor: _Actor, scope: AccessScope) -> None:
    """May `actor` create or change records in `scope`? The one rule every CRM service applies
    before writing (403 otherwise; the scope itself was already authorised for reading).

    - own workspace (SELF): CRM_ACCESS_OWN;
    - another user's workspace or the organisation (USER, ORGANIZATION): CRM_MANAGE_ANY.
      Being allowed to *view* someone's workspace never implies being allowed to change it.
    """
    if not actor.is_authenticated or not actor.is_active or scope.actor_id != actor.pk:
        raise PermissionDeniedError()
    required = (
        Capability.CRM_ACCESS_OWN if scope.kind is ScopeKind.SELF else Capability.CRM_MANAGE_ANY
    )
    if not has_capability(actor, required):
        raise PermissionDeniedError()


def _record_delegated_access(scope: AccessScope) -> None:
    """Audit access to other users' data: once per actor/workspace per window.

    Writes inside the workspace are audited individually by the services performing them;
    this records the *viewing*. The window's marker is a database row inserted in the same
    transaction as the audit row (identity.models.WorkspaceAccessWindow): a rolled-back
    request leaves neither, so it never suppresses auditing, and of several requests
    opening a window at once exactly one writes the row.
    """
    subject = str(scope.subject_user_id) if scope.subject_user_id else WORKSPACE_ORGANIZATION
    update_context(subject_user_id=subject)
    window = settings.WORKSPACE_ACCESS_AUDIT_WINDOW_S
    epoch = int(timezone.now().timestamp())
    window_start = datetime.fromtimestamp(epoch - epoch % window, tz=UTC)
    marker = {"actor_id": scope.actor_id, "workspace": subject, "window_start": window_start}
    if WorkspaceAccessWindow.objects.filter(**marker).exists():
        return  # already audited in this window: one read, no write
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO identity_workspace_access_window (actor_id, workspace, window_start)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING id",
            [scope.actor_id, subject, window_start],
        )
        if cursor.fetchone() is None:
            return  # already audited in this window
        audit.record(
            AUDIT_ACTION_WORKSPACE_ACCESSED,
            actor_id=scope.actor_id,
            target_type="workspace",
            target_id=subject,
            subject_user_id=scope.subject_user_id,
            metadata={"scope": scope.kind.value},
        )


def purge_access_windows(now: datetime) -> int:
    """Hourly: windows that ended are no longer needed."""
    ended = now - timedelta(seconds=2 * settings.WORKSPACE_ACCESS_AUDIT_WINDOW_S)
    deleted, _ = WorkspaceAccessWindow.objects.filter(window_start__lt=ended).delete()
    return deleted
