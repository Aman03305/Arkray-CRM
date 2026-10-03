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
from typing import Protocol
from uuid import UUID

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

from arkray.audit import services as audit
from arkray.core.access import AccessScope, ScopeKind
from arkray.core.context import update_context
from arkray.core.errors import NotFoundError, PermissionDeniedError

from .models import User
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


def resolve_workspace(actor: _Actor, workspace: str) -> AccessScope:
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDeniedError()

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
    """Audit access to other users' data: at least once per actor/workspace per window.

    Writes inside the workspace are audited individually by the services performing them;
    this records the *viewing*. The window marker is set only after the audit row has
    committed, so a rolled-back request never suppresses auditing. If the cache is
    unavailable, every access is audited: we fail towards more auditing, never less.
    """
    subject = str(scope.subject_user_id) if scope.subject_user_id else WORKSPACE_ORGANIZATION
    update_context(subject_user_id=subject)
    key = f"audit:workspace-access:{scope.actor_id}:{subject}"
    try:
        audited_in_window = cache.get(key) is not None
    except Exception:  # noqa: BLE001 — cache trouble must never skip auditing
        audited_in_window = False
    if audited_in_window:
        return
    audit.record(
        AUDIT_ACTION_WORKSPACE_ACCESSED,
        actor_id=scope.actor_id,
        target_type="workspace",
        target_id=subject,
        subject_user_id=scope.subject_user_id,
        metadata={"scope": scope.kind.value},
    )
    transaction.on_commit(lambda: _mark_audited(key))


def _mark_audited(key: str) -> None:
    try:
        cache.set(key, 1, timeout=settings.WORKSPACE_ACCESS_AUDIT_WINDOW_S)
    except Exception:  # noqa: BLE001 — worst case the next access is audited again
        logger.warning("workspace_audit_window_not_recorded")
