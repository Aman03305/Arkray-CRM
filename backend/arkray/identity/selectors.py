"""Read queries for identity. Callers are already authorised (views check capabilities,
workspaces are resolved to an AccessScope before anything here runs)."""

from __future__ import annotations

from uuid import UUID

from django.db import connection
from django.db.models import F, FilteredRelation, Q, QuerySet

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
