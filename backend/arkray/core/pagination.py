"""Default list pagination: cursor (keyset) based, bounded page size.

Keyset pagination stays fast on large tables and is stable while rows are being inserted.
Views that need a different stable ordering override `ordering`; it must end in a unique
column (id) so the cursor is unambiguous.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from rest_framework.exceptions import NotFound
from rest_framework.pagination import CursorPagination
from rest_framework.request import Request
from rest_framework.views import APIView


class DefaultCursorPagination(CursorPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100
    ordering = ("-created_at", "-id")

    def paginate_queryset(
        self, queryset: QuerySet[Any], request: Request, view: APIView | None = None
    ) -> list[Any] | None:
        # DRF validates the cursor's encoding but not the position inside it; a forged
        # position ("p=not-a-date") would otherwise surface as a 500 from the query.
        try:
            return super().paginate_queryset(queryset, request, view)
        except (ValidationError, ValueError, TypeError, OverflowError):
            raise NotFound(self.invalid_cursor_message) from None
