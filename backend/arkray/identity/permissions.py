"""DRF permission classes. Views declare one of these explicitly (the default is DenyAll).

An anonymous or deactivated caller fails authentication first and gets 401; a signed-in
caller lacking the capability gets 403. The authorization-matrix test
(tests/architecture) checks that each route declares exactly the rule the matrix states.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rest_framework.permissions import BasePermission

from arkray.core.errors import PasswordChangeRequired, SupportSessionActive

from .policy import Capability, has_capability

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView


def _account_gates(request: Request, view: APIView) -> None:
    """Applied after the caller passed their permission check, on every route:

    - a user who must change an administrator-chosen password reaches only the views marked
      `allowed_before_password_change` (who am I, change password);
    - an administrator in a support session can't use views marked
      `refused_in_support_session` (user administration, passwords, security events):
      identity and security work needs their normal administrator context."""
    if getattr(request.user, "password_change_required", False) and not getattr(
        view, "allowed_before_password_change", False
    ):
        raise PasswordChangeRequired()
    if getattr(view, "refused_in_support_session", False) and (
        getattr(request, "support_session", None) is not None
    ):
        raise SupportSessionActive()


class IsActiveUser(BasePermission):
    """Any signed-in, active user, acting on their own account or a resolved workspace."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        if not (user and user.is_authenticated and user.is_active):
            return False
        _account_gates(request, view)
        return True


class HasCapability(BasePermission):
    """Base for `requires(capability)`; never used directly."""

    capability: ClassVar[Capability | None] = None

    def has_permission(self, request: Request, view: APIView) -> bool:
        if self.capability is None or not has_capability(request.user, self.capability):
            return False
        _account_gates(request, view)
        return True


_CAPABILITY_PERMISSIONS: dict[Capability, type[HasCapability]] = {}


def requires(capability: Capability) -> type[HasCapability]:
    """The permission class granting access to holders of `capability`."""
    if capability not in _CAPABILITY_PERMISSIONS:
        _CAPABILITY_PERMISSIONS[capability] = type(
            f"Requires[{capability.value}]", (HasCapability,), {"capability": capability}
        )
    return _CAPABILITY_PERMISSIONS[capability]
