"""Identity API views: thin HTTP adapters over identity.authentication and
identity.services. Every view declares its permission explicitly (the global default is
DenyAll), and every route is listed in tests/authz_matrix.py."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.contrib.auth import update_session_auth_hash
from django.middleware.csrf import get_token
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, ScopedRateThrottle, UserRateThrottle

from arkray.core.api import ApiView
from arkray.core.errors import InvalidInputError, PermissionDeniedError
from arkray.core.keyset import (
    CursorBinding,
    KeysetOrdering,
    KeysetPaginator,
    SortKey,
    page_links,
)
from arkray.core.middleware import client_ip

from .. import authentication, selectors, services, throttling
from ..models import Role, User, UserStatus
from ..permissions import IsActiveUser, requires
from ..policy import Capability
from ..workspaces import resolve_workspace
from . import serializers as s

RESET_REQUEST_ACCEPTED = (
    "If an eligible account exists, password reset instructions have been sent."
)
# Redis-backed request-rate limits (fail open in an outage). Brute-force protection does
# not depend on them: it is the PostgreSQL-backed throttle in identity.throttling.
AUTH_THROTTLES = [AnonRateThrottle, UserRateThrottle, ScopedRateThrottle]


def _validated[S: serializers.Serializer[Any]](
    serializer_class: type[S], data: Any
) -> dict[str, Any]:
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


def _actor(request: Request) -> User:
    user = request.user
    if not isinstance(user, User):  # unreachable behind the permission classes
        raise PermissionDeniedError()
    return user


# --- authentication ---------------------------------------------------------------------------
class CsrfCookieView(ApiView):
    """Issue the CSRF cookie the SPA echoes in X-CSRFToken."""

    permission_classes = [AllowAny]

    @extend_schema(responses={204: None})
    def get(self, request: Request) -> Response:
        get_token(request._request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class LoginView(ApiView):
    permission_classes = [AllowAny]
    throttle_classes = AUTH_THROTTLES
    throttle_scope = "auth"

    @extend_schema(request=s.LoginSerializer, responses={200: s.ViewerSerializer})
    def post(self, request: Request) -> Response:
        data = _validated(s.LoginSerializer, request.data)
        result = authentication.sign_in(request._request, data["email"], data["password"])
        response = Response(s.ViewerSerializer(result.user).data)
        throttling.remember_device(
            response, result.identifier, result.device_id, result.device_trust
        )
        return response


class LogoutView(ApiView):
    """Ends the session. Idempotent: signing out without a session is a no-op."""

    permission_classes = [AllowAny]

    @extend_schema(request=None, responses={204: None})
    def post(self, request: Request) -> Response:
        authentication.sign_out(request._request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(ApiView):
    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: s.ViewerSerializer})
    def get(self, request: Request) -> Response:
        return Response(s.ViewerSerializer(_actor(request)).data)


class PasswordChangeView(ApiView):
    permission_classes = [IsActiveUser]
    throttle_classes = AUTH_THROTTLES
    throttle_scope = "auth"

    @extend_schema(request=s.PasswordChangeSerializer, responses={204: None})
    def post(self, request: Request) -> Response:
        data = _validated(s.PasswordChangeSerializer, request.data)
        authentication.change_password(
            request._request, data["current_password"], data["new_password"]
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetRequestView(ApiView):
    permission_classes = [AllowAny]
    throttle_classes = AUTH_THROTTLES
    throttle_scope = "auth"

    @extend_schema(request=s.PasswordResetRequestSerializer, responses={202: s.DetailSerializer})
    def post(self, request: Request) -> Response:
        data = _validated(s.PasswordResetRequestSerializer, request.data)
        services.request_password_reset(data["email"], ip=client_ip(request._request))
        return Response({"detail": RESET_REQUEST_ACCEPTED}, status=status.HTTP_202_ACCEPTED)


class PasswordResetConfirmView(ApiView):
    permission_classes = [AllowAny]
    throttle_classes = AUTH_THROTTLES
    throttle_scope = "auth"

    @extend_schema(request=s.PasswordResetConfirmSerializer, responses={204: None})
    def post(self, request: Request) -> Response:
        data = _validated(s.PasswordResetConfirmSerializer, request.data)
        services.confirm_password_reset(data["token"], data["new_password"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class InvitationVerifyView(ApiView):
    """Who a still-valid invitation link is for. POST so the secret never enters a URL."""

    permission_classes = [AllowAny]
    throttle_classes = AUTH_THROTTLES
    throttle_scope = "auth"

    @extend_schema(
        request=s.InvitationTokenSerializer, responses={200: s.InvitationPreviewSerializer}
    )
    def post(self, request: Request) -> Response:
        data = _validated(s.InvitationTokenSerializer, request.data)
        user = services.inspect_invitation(data["token"])
        return Response({"email": user.email, "first_name": user.first_name})


class InvitationAcceptView(ApiView):
    permission_classes = [AllowAny]
    throttle_classes = AUTH_THROTTLES
    throttle_scope = "auth"

    @extend_schema(
        request=s.InvitationAcceptSerializer, responses={200: s.InvitationPreviewSerializer}
    )
    def post(self, request: Request) -> Response:
        data = _validated(s.InvitationAcceptSerializer, request.data)
        user = services.accept_invitation(data["token"], data["password"])
        return Response({"email": user.email, "first_name": user.first_name})


# --- user administration (users.manage) -----------------------------------------------------
ADMIN_USER_ORDERING = KeysetOrdering(
    "-created_at", (SortKey("created_at", descending=True), SortKey("id", descending=True))
)
ADMIN_USER_PAGE_SIZE = 25


class AdminUserListView(ApiView):
    permission_classes = [requires(Capability.USERS_MANAGE)]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="admin_users_list",
        parameters=[s.UserListQuerySerializer],
        responses={200: s.AdminUserPageSerializer},
    )
    def get(self, request: Request) -> Response:
        params = _validated(s.UserListQuerySerializer, request.query_params)
        users = selectors.admin_user_list(
            q=params.get("q", ""),
            status=UserStatus(params["status"]) if "status" in params else None,
            role=Role(params["role"]) if "role" in params else None,
        )
        # Signed, bound and expiring page links like every other list (Phase 9 review: DRF's
        # cursor was readable, unbound and never expired).
        page = KeysetPaginator(
            ADMIN_USER_ORDERING,
            page_size=params.get("page_size", ADMIN_USER_PAGE_SIZE),
            binding=CursorBinding.of("users.admin_list", params, actor_id=_actor(request).pk),
        ).paginate(users, params.get("cursor"))
        return Response(
            {
                "results": s.AdminUserSerializer(page.items, many=True).data,
                **page_links(request, page),
            }
        )

    @extend_schema(request=s.UserCreateSerializer, responses={201: s.AdminUserSerializer})
    def post(self, request: Request) -> Response:
        data = _validated(s.UserCreateSerializer, request.data)
        user = services.create_user(actor_id=_actor(request).pk, **data)
        response = Response(s.AdminUserSerializer(user).data, status=status.HTTP_201_CREATED)
        response["Location"] = f"/api/v1/admin/users/{user.pk}"
        return response


class AdminUserDetailView(ApiView):
    permission_classes = [requires(Capability.USERS_MANAGE)]

    @extend_schema(responses={200: s.AdminUserSerializer})
    def get(self, request: Request, user_id: UUID) -> Response:
        return Response(s.AdminUserSerializer(selectors.admin_user_detail(user_id)).data)

    @extend_schema(request=s.UserUpdateSerializer, responses={200: s.AdminUserSerializer})
    def patch(self, request: Request, user_id: UUID) -> Response:
        data = _validated(s.UserUpdateSerializer, request.data)
        version = data.pop("version")
        user = services.update_user(
            actor_id=_actor(request).pk, user_id=user_id, version=version, changes=data
        )
        return Response(s.AdminUserSerializer(user).data)


class AdminUserEmailView(ApiView):
    permission_classes = [requires(Capability.USERS_MANAGE)]

    @extend_schema(request=s.EmailChangeSerializer, responses={200: s.AdminUserSerializer})
    def post(self, request: Request, user_id: UUID) -> Response:
        data = _validated(s.EmailChangeSerializer, request.data)
        actor = _actor(request)
        if user_id == actor.pk:
            # Your own sign-in identity: prove it's you, so a hijacked session can't be
            # turned into a permanent account takeover.
            if not data.get("current_password"):
                message = "Enter your current password to change your own email."
                raise InvalidInputError(message, details={"current_password": [message]})
            authentication.verify_current_password(request._request, data["current_password"])
        own = user_id == actor.pk
        user = services.change_user_email(
            actor_id=actor.pk,
            user_id=user_id,
            version=data["version"],
            email=data["email"],
            # Your own identity: refused if this session ended meanwhile (a reset, say).
            still_signed_in=(
                (lambda locked: authentication.session_still_current(request._request, locked))
                if own
                else None
            ),
        )
        if own:
            # Changing your own email ends your other sessions but keeps this one.
            update_session_auth_hash(request._request, user)
        return Response(s.AdminUserSerializer(user).data)


class AdminUserDeactivateView(ApiView):
    permission_classes = [requires(Capability.USERS_MANAGE)]

    @extend_schema(request=None, responses={200: s.AdminUserSerializer})
    def post(self, request: Request, user_id: UUID) -> Response:
        user = services.deactivate_user(actor_id=_actor(request).pk, user_id=user_id)
        return Response(s.AdminUserSerializer(user).data)


class AdminUserActivateView(ApiView):
    permission_classes = [requires(Capability.USERS_MANAGE)]

    @extend_schema(request=None, responses={200: s.AdminUserSerializer})
    def post(self, request: Request, user_id: UUID) -> Response:
        user = services.reactivate_user(actor_id=_actor(request).pk, user_id=user_id)
        return Response(s.AdminUserSerializer(user).data)


class AdminUserResendInvitationView(ApiView):
    permission_classes = [requires(Capability.USERS_MANAGE)]

    @extend_schema(request=None, responses={200: s.AdminUserSerializer})
    def post(self, request: Request, user_id: UUID) -> Response:
        user = services.resend_invitation(actor_id=_actor(request).pk, user_id=user_id)
        return Response(s.AdminUserSerializer(user).data)


# --- assignees (crm.assign_any) ----------------------------------------------------------------
# Names are private sort keys: never copied into cursors (URLs), see core.keyset.
ASSIGNEE_ORDERING = KeysetOrdering(
    "name",
    (SortKey("first_name", private=True), SortKey("last_name", private=True), SortKey("id")),
)


class AssigneeListView(ApiView):
    """Who CRM records can be assigned to (owner pickers). Active users only, by name."""

    permission_classes = [requires(Capability.CRM_ASSIGN_ANY)]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="assignees_list",
        parameters=[s.AssigneeQuerySerializer],
        responses={200: s.AssigneePageSerializer},
    )
    def get(self, request: Request) -> Response:
        params = _validated(s.AssigneeQuerySerializer, request.query_params)
        users = selectors.assignable_users(q=params.get("q", ""))
        binding = CursorBinding.of("users.assignees", params, actor_id=_actor(request).pk)
        page = KeysetPaginator(
            ASSIGNEE_ORDERING, page_size=params["page_size"], binding=binding
        ).paginate(users, params.get("cursor"))
        return Response(
            {
                "results": s.AssigneeSerializer(page.items, many=True).data,
                **page_links(request, page),
            }
        )


# --- workspaces -------------------------------------------------------------------------------
class WorkspaceView(ApiView):
    """Describe a workspace (whose CRM it is). Opening another user's workspace is
    authorised and audited by resolve_workspace; outside the caller's reach it is 404."""

    permission_classes = [IsActiveUser]

    @extend_schema(responses={200: s.WorkspaceSerializer, 404: OpenApiResponse()})
    def get(self, request: Request, workspace: str) -> Response:
        scope = resolve_workspace(_actor(request), workspace)
        subject = selectors.workspace_subject(scope)
        return Response(s.WorkspaceSerializer({"kind": scope.kind.value, "subject": subject}).data)
