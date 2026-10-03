"""DRF permission classes. Views declare one of these explicitly (the default is DenyAll).

An anonymous or deactivated caller fails authentication first and gets 401; a signed-in
caller lacking the capability gets 403. The authorization-matrix test
(tests/architecture) checks that each route declares exactly the rule the matrix states.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rest_framework.permissions import BasePermission

from .policy import Capability, has_capability

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView


class IsActiveUser(BasePermission):
    """Any signed-in, active user, acting on their own account or a resolved workspace."""

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and user.is_active)


class HasCapability(BasePermission):
    """Base for `requires(capability)`; never used directly."""

    capability: ClassVar[Capability | None] = None

    def has_permission(self, request: Request, view: APIView) -> bool:
        return self.capability is not None and has_capability(request.user, self.capability)


_CAPABILITY_PERMISSIONS: dict[Capability, type[HasCapability]] = {}


def requires(capability: Capability) -> type[HasCapability]:
    """The permission class granting access to holders of `capability`."""
    if capability not in _CAPABILITY_PERMISSIONS:
        _CAPABILITY_PERMISSIONS[capability] = type(
            f"Requires[{capability.value}]", (HasCapability,), {"capability": capability}
        )
    return _CAPABILITY_PERMISSIONS[capability]
