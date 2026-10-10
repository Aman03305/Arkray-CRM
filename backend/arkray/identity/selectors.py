"""Read queries for identity. Callers are already authorised (views check capabilities,
workspaces are resolved to an AccessScope before anything here runs)."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from uuid import UUID

from django.db import connection
from django.db.models import F, FilteredRelation, Q, QuerySet

from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.core.errors import NotFoundError
from arkray.core.text import search_terms

from .models import Role, TokenPurpose, TokenStatus, User, UserStatus
from .policy import Capability, roles_with


def _with_pending_invitation(queryset: QuerySet[User]) -> QuerySet[User]:
    """Annotate the live invitation (at most one per user: a DB constraint) in one join."""
    return queryset.annotate(
        pending_invitation=FilteredRelation(
            "account_tokens",
            condition=Q(
                account_tokens__purpose=TokenPurpose.INVITATION,
                account_tokens__status=TokenStatus.PENDING,
            ),
        ),
        invitation_expires_at=F("pending_invitation__expires_at"),
        invitation_sent_at=F("pending_invitation__sent_at"),
    )


def admin_user_list(
    *, q: str = "", status: UserStatus | None = None, role: Role | None = None
) -> QuerySet[User]:
    """Users for the admin table. Each search term matches first name, last name or email
    (case-insensitive substring, served by trigram indexes). Unpaginated: the view
    paginates with a keyset cursor."""
    queryset = User.objects.all()
    for term in search_terms(q):
        queryset = queryset.filter(
            Q(first_name__icontains=term) | Q(last_name__icontains=term) | Q(email__icontains=term)
        )
    if status is not None:
        queryset = queryset.filter(status=status)
    if role is not None:
        queryset = queryset.filter(role=role)
    return _with_pending_invitation(queryset)


def admin_user_detail(user_id: UUID) -> User:
    user = _with_pending_invitation(User.objects.filter(pk=user_id)).first()
    if user is None:
        raise NotFoundError()
    return user


def workspace_subject(scope: AccessScope) -> User | None:
    """The user whose workspace this is, or None for the organisation-wide workspace."""
    subject_id = scope.subject_user_id
    if subject_id is None:
        return None
    user = (
        User.objects.filter(pk=subject_id)
        .only("id", "first_name", "last_name", "email", "status")
        .first()
    )
    if user is None:  # deleted between resolution and now (cannot happen: no deletes)
        raise NotFoundError()
    return user


def assignable_users(*, q: str = "") -> QuerySet[User]:
    """Users CRM records may be assigned to: active accounts whose role works in a CRM
    workspace. Each search term matches first name, last name or email. Unpaginated."""
    queryset = User.objects.filter(
        status=UserStatus.ACTIVE, role__in=roles_with(Capability.CRM_ACCESS_OWN)
    ).only("id", "first_name", "last_name", "email")
    for term in search_terms(q):
        queryset = queryset.filter(
            Q(first_name__icontains=term) | Q(last_name__icontains=term) | Q(email__icontains=term)
        )
    return queryset


def lock_assignable_user(user_id: UUID) -> bool:
    """True if `user_id` may own CRM records, holding a share lock on the user row until the
    caller's transaction ends. A concurrent deactivation or role change (which locks the row
    for update) therefore waits for the assignment to commit, or the assignment sees its
    result: a record is never assigned to someone who stopped being assignable meanwhile."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT role FROM identity_user WHERE id = %s AND status = %s FOR SHARE",
            [user_id, UserStatus.ACTIVE],
        )
        row = cursor.fetchone()
    return row is not None and row[0] in roles_with(Capability.CRM_ACCESS_OWN)


def people(user_ids: Iterable[UUID]) -> dict[UUID, User]:
    """Users by id with only what names them (first and last name, active state): for
    labelling owners in aggregates. Never the email, role or any security field."""
    ids = set(user_ids)
    if not ids:
        return {}
    users = User.objects.filter(pk__in=ids).only("id", "first_name", "last_name", "is_active")
    return {user.pk: user for user in users}


# --- security events (docs/authorization.md#password-change-notification) ---------------------
# What administrators are told about accounts: who did what to which account, and when. An
# allowlist of actions; never a password, a hash, a token or a link. The keys of metadata
# shown are allowlisted per action too.
SECURITY_ACTIONS: dict[str, tuple[str, ...]] = {
    "auth.login_with_temporary_password": (),
    "auth.password_changed": (),
    "auth.password_reset_completed": (),
    "auth.password_set_by_admin": (),
    "user.created": ("role", "activation"),
    "user.deactivated": (),
    "user.reactivated": (),
    "user.role_changed": ("from", "to"),
    "user.email_changed": (),
    "support_session.started": ("reason",),
    "support_session.ended": ("end",),
}


def security_events() -> QuerySet[AuditEvent]:
    """Recent account and security events, newest first (paginated by the caller). With
    each its expiring detail while it is kept (a support session's reason)."""
    return (
        AuditEvent.objects.filter(action__in=list(SECURITY_ACTIONS))
        .select_related("detail")
        .only(
            "id",
            "occurred_at",
            "action",
            "actor_id",
            "target_type",
            "target_id",
            "subject_user_id",
            "metadata",
            "support_session_id",
            "detail__values",
        )
    )


def event_details(event: AuditEvent) -> dict[str, object]:
    """An event's metadata and, while kept, its detail's values (the detail's wins)."""
    try:
        values = event.detail.values
    except AuditEvent.detail.RelatedObjectDoesNotExist:
        values = {}
    return {**(event.metadata or {}), **(values or {})}


def people_by_id(user_ids: Collection[UUID]) -> dict[UUID, User]:
    """Names of the given users (one query), for lists that reference users by id."""
    if not user_ids:
        return {}
    return {
        user.pk: user
        for user in User.objects.filter(pk__in=list(user_ids)).only(
            "id", "first_name", "last_name", "is_active"
        )
    }
