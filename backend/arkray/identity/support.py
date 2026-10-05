"""Support sessions: an administrator's time-limited, audited access to one user's CRM
(docs/admin-user-workspace.md#support-sessions).

The business asked for "Admin can log in as any user". That is satisfied without anyone
learning, using or resetting the user's password, and without impersonation:

- the administrator stays signed in as themselves (their credentials, their browser session,
  `/auth/me` still says who they are);
- they start a support session for one *active, non-administrator* user, optionally saying
  why; it is bound to their current browser session (a digest of its key) and lasts
  SUPPORT_SESSION_TTL_S (30 minutes by default), with no extension;
- while it lasts, every request can open only that user's workspace (identity.workspaces),
  identity and security operations (user administration, passwords, security events, another
  support session) are refused (permissions._account_gates), and every audit event, stage
  transition and negotiated price written carries the session's id, with the administrator
  as the actor and the user as the subject: nothing is ever recorded as the user's own act;
- it ends when the administrator exits, signs out, or its browser session changes, when it
  expires, or when it may no longer continue (the user was deactivated or made an
  administrator, or the administrator lost the capability): checked on every request, so
  nothing outlives its conditions. An hourly sweep closes expired sessions nobody used again.
Starting and ending are audited (`support_session.started` / `.ended`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core.context import update_context
from arkray.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from arkray.core.text import TextRejected, clean_line

from .models import SupportEnd, SupportSession, User, UserStatus
from .policy import Capability, has_capability, roles_with

SESSION_KEY = "_arkray_support"
AUDIT_STARTED = "support_session.started"
AUDIT_ENDED = "support_session.ended"
REASON_MAX_LENGTH = 200

ALREADY_ACTIVE = "You're already in a support session. Exit it first."
NOT_ACTIVE_USER = "Support sessions are for active users. This user can't sign in right now."
NOT_FOR_ADMINS = "Support sessions are for CRM users, not administrators."
NO_CRM = "This user has no CRM workspace."


def _digest(session_key: str | None) -> str:
    return hashlib.sha256((session_key or "").encode()).hexdigest()


def _is_manager(user: User) -> bool:
    return user.role in roles_with(Capability.USERS_MANAGE)


def _clean_reason(reason: str) -> str:
    try:
        text = clean_line(reason)
    except TextRejected as exc:
        raise InvalidInputError(details={"reason": [str(exc)]}) from None
    if len(text) > REASON_MAX_LENGTH:
        raise InvalidInputError(
            details={"reason": [f"Use at most {REASON_MAX_LENGTH} characters."]}
        )
    return text


def _end(session: SupportSession, reason: SupportEnd, *, actor_id: UUID | None) -> bool:
    """End a live session (idempotent: only the first end counts and is audited). The actor
    is the administrator when they ended it, the system otherwise. The end and its audit
    event commit together: an ended session always has its event."""
    now = timezone.now()
    with transaction.atomic():
        ended = SupportSession.objects.filter(pk=session.pk, ended_at__isnull=True).update(
            ended_at=now, end_reason=reason
        )
        if not ended:
            return False
        audit.record(
            AUDIT_ENDED,
            actor_id=actor_id,
            target_type="user",
            target_id=session.target_id,
            subject_user_id=session.target_id,
            metadata={"admin_id": str(session.admin_id), "end": reason.value},
            support_session_id=session.pk,
        )
    session.ended_at, session.end_reason = now, reason
    return True


def start(request: HttpRequest, actor: User, target_id: UUID, reason: str = "") -> SupportSession:
    """Start a support session for `target_id` in this browser session. Any earlier live
    session of the administrator (elsewhere) ends first: at most one at a time."""
    if not has_capability(actor, Capability.SUPPORT_ACCESS):
        raise PermissionDeniedError()
    if getattr(request, "support_session", None) is not None:
        raise ConflictError(ALREADY_ACTIVE)
    clean_reason = _clean_reason(reason)
    session_key = request.session.session_key
    if not session_key:
        raise PermissionDeniedError()
    try:
        with transaction.atomic():
            target = User.objects.filter(pk=target_id).first()
            if target is None or target.pk == actor.pk:
                raise NotFoundError()
            if target.status != UserStatus.ACTIVE:
                raise BusinessRuleViolation(NOT_ACTIVE_USER)
            if _is_manager(target):
                raise BusinessRuleViolation(NOT_FOR_ADMINS)
            if not has_capability(target, Capability.CRM_ACCESS_OWN):
                raise BusinessRuleViolation(NO_CRM)
            for earlier in SupportSession.objects.filter(admin=actor, ended_at__isnull=True):
                _end(earlier, SupportEnd.SESSION_CHANGED, actor_id=actor.pk)
            now = timezone.now()
            session = SupportSession.objects.create(
                admin=actor,
                target=target,
                reason=clean_reason,
                session_digest=_digest(session_key),
                started_at=now,
                expires_at=now + timedelta(seconds=settings.SUPPORT_SESSION_TTL_S),
            )
            audit.record(
                AUDIT_STARTED,
                actor_id=actor.pk,
                target_type="user",
                target_id=target.pk,
                subject_user_id=target.pk,
                metadata={"reason": clean_reason, "expires_at": session.expires_at.isoformat()},
                support_session_id=session.pk,
            )
    except IntegrityError:
        # Another request of the same administrator started one at the same moment.
        raise ConflictError(ALREADY_ACTIVE) from None
    request.session[SESSION_KEY] = str(session.pk)
    return SupportSession.objects.select_related("target").get(pk=session.pk)


def exit_session(request: HttpRequest) -> None:
    """End this browser session's support session (no-op without one)."""
    session = getattr(request, "support_session", None)
    user = request.user
    if session is not None and isinstance(user, User):
        _end(session, SupportEnd.EXITED, actor_id=user.pk)
    request.session.pop(SESSION_KEY, None)
    request.support_session = None  # type: ignore[attr-defined]


def end_on_sign_out(request: HttpRequest) -> None:
    raw = request.session.get(SESSION_KEY)
    user = request.user
    if not raw or not isinstance(user, User):
        return
    session = SupportSession.objects.filter(pk=raw, admin=user, ended_at__isnull=True).first()
    if session is not None:
        _end(session, SupportEnd.SIGNED_OUT, actor_id=user.pk)


def resolve(request: HttpRequest, now: datetime) -> SupportSession | None:
    """This request's live support session, or None. A session that may not continue is
    ended (and audited) here, so it never outlives its conditions by more than a request."""
    raw = request.session.get(SESSION_KEY)
    if not raw:
        return None
    user = request.user
    try:
        session_id = UUID(str(raw))
    except ValueError:
        session_id = None
    session = (
        SupportSession.objects.select_related("target").filter(pk=session_id).first()
        if isinstance(user, User) and session_id is not None
        else None
    )
    if session is None or not isinstance(user, User) or session.admin_id != user.pk:
        request.session.pop(SESSION_KEY, None)
        return None
    if session.ended_at is not None:
        request.session.pop(SESSION_KEY, None)
        return None
    problem: SupportEnd | None = None
    if now >= session.expires_at:
        problem = SupportEnd.EXPIRED
    elif session.session_digest != _digest(request.session.session_key):
        problem = SupportEnd.SESSION_CHANGED
    elif (
        not user.is_active
        or not has_capability(user, Capability.SUPPORT_ACCESS)
        or session.target.status != UserStatus.ACTIVE
        or _is_manager(session.target)
    ):
        problem = SupportEnd.NOT_ALLOWED
    if problem is not None:
        _end(session, problem, actor_id=None)
        request.session.pop(SESSION_KEY, None)
        return None
    return session


def sweep_expired(now: datetime) -> int:
    """Close live sessions past their expiry that nobody used again (hourly). Idempotent."""
    ended = 0
    for session in SupportSession.objects.filter(ended_at__isnull=True, expires_at__lte=now):
        ended += _end(session, SupportEnd.EXPIRED, actor_id=None)
    return ended


class SupportSessionMiddleware:
    """Attach the live support session (if any) to API requests, and put its id and target
    into the execution context: audit events and history rows record it, and workspace
    resolution allows only the target's CRM. After authentication and the session policy."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.support_session = None  # type: ignore[attr-defined]
        if request.path.startswith("/api/") and SESSION_KEY in request.session:
            session = resolve(request, timezone.now())
            if session is not None:
                request.support_session = session  # type: ignore[attr-defined]
                update_context(
                    support_session_id=str(session.pk), support_target_id=str(session.target_id)
                )
        return self.get_response(request)
