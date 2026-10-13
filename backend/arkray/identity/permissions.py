"""DRF permission classes. Views declare one of these explicitly (the default is DenyAll).

An anonymous or deactivated caller fails authentication first and gets 401; a signed-in
caller lacking the capability gets 403. The authorization-matrix test
(tests/architecture) checks that each route declares exactly the rule the matrix states.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rest_framework.permissions import BasePermission

from arkray.core.errors import (
    MfaEnrollmentRequired,
    MfaRequired,
    PasswordChangeRequired,
    SupportSessionActive,
)

from . import mfa_services
from .policy import Capability, has_any_capability

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView


def _account_gates(request: Request, view: APIView) -> None:
    """Applied after the caller passed their permission check, on every route:

    - a user who must change an administrator-chosen password reaches only the views marked
      `allowed_before_password_change` (who am I, change password);
    - an administrator in a support session can't use views marked
      `refused_in_support_session` (user administration, passwords, security events):
      identity and security work needs their normal administrator context;
    - an administrator who must have a second factor and hasn't set one up reaches only the
      views marked `allowed_before_mfa_enrollment` (who am I, change password, two-step
      setup): 403 `mfa_enrollment_required` (ADR-0033);
    - views marked `requires_recent_mfa` (True: every method; or a set of methods) need a
      code verified in this session within MFA_STEP_UP_TTL_S from a caller who has a second
      factor: 403 `mfa_required` otherwise (POST /auth/mfa/verify, then repeat)."""
    if getattr(request.user, "password_change_required", False) and not getattr(
        view, "allowed_before_password_change", False
    ):
        raise PasswordChangeRequired()
    if getattr(view, "refused_in_support_session", False) and (
        getattr(request, "support_session", None) is not None
    ):
        raise SupportSessionActive()
    user = request.user
    if not getattr(view, "allowed_before_mfa_enrollment", False) and (
        mfa_services.enrollment_required(user)
    ):
        raise MfaEnrollmentRequired()
    if needs_recent_mfa(view, request.method or "") and not mfa_services.recently_verified(
        request, user
    ):
        raise MfaRequired()


def needs_recent_mfa(view: object, method: str) -> bool:
    """Whether `view` marks `method` as needing a recent second-factor verification."""
    marked = getattr(view, "requires_recent_mfa", False)
    if marked is True:
        return True
    if isinstance(marked, frozenset | set | tuple):
        return method.upper() in marked
    return False


class IsActiveUser(BasePermission):
    """Any signed-in, active user, acting on their own account or a resolved workspace."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        if not (user and user.is_authenticated and user.is_active):
            return False
        _account_gates(request, view)
        return True


class HasCapability(BasePermission):
    """Base for `requires(capability)` and `requires_any(...)`; never used directly."""

    capability: ClassVar[Capability | None] = None
    # requires_any: holders of any one of these pass (the services then apply its reach).
    any_of: ClassVar[tuple[Capability, ...]] = ()

    def has_permission(self, request: Request, view: APIView) -> bool:
        wanted = self.any_of or ((self.capability,) if self.capability is not None else ())
        if not wanted or not has_any_capability(request.user, *wanted):
            return False
        _account_gates(request, view)
        return True


_CAPABILITY_PERMISSIONS: dict[tuple[Capability, ...], type[HasCapability]] = {}


def requires(capability: Capability) -> type[HasCapability]:
    """The permission class granting access to holders of `capability`."""
    key = (capability,)
    if key not in _CAPABILITY_PERMISSIONS:
        _CAPABILITY_PERMISSIONS[key] = type(
            f"Requires[{capability.value}]", (HasCapability,), {"capability": capability}
        )
    return _CAPABILITY_PERMISSIONS[key]


def requires_any(*capabilities: Capability) -> type[HasCapability]:
    """The permission class granting access to holders of any of `capabilities`: an
    organisation-wide capability and its team counterpart (ADR-0035). Passing it says only
    that the caller may ask; the service decides whom their reach covers."""
    if len(capabilities) < 2:  # one capability is `requires`
        raise ValueError("requires_any needs at least two capabilities; use requires().")
    key = tuple(capabilities)
    if key not in _CAPABILITY_PERMISSIONS:
        _CAPABILITY_PERMISSIONS[key] = type(
            f"RequiresAny[{'|'.join(c.value for c in capabilities)}]",
            (HasCapability,),
            {"any_of": key},
        )
    return _CAPABILITY_PERMISSIONS[key]
