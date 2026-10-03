"""Deny-by-default permission baseline.

`DenyAll` is the global DRF default, so a view that forgets to declare permissions is
unreachable rather than accidentally public.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rest_framework.permissions import BasePermission

if TYPE_CHECKING:  # runtime import would be circular (DRF loads this module from its views)
    from rest_framework.request import Request
    from rest_framework.views import APIView


class DenyAll(BasePermission):
    def has_permission(self, request: Request, view: APIView) -> bool:
        return False

    def has_object_permission(self, request: Request, view: APIView, obj: Any) -> bool:
        return False
