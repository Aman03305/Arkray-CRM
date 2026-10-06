"""Global search API view: a thin HTTP adapter over search.selectors.

Nested under /api/v1/workspaces/{workspace}/ like every CRM route: the workspace resolves to
an AccessScope first (404 for workspaces the caller may not open, before the query is even
validated; delegated access is audited by resolve_workspace, once per window, not per
search), and every record is searched inside that scope. The same view serves a
salesperson's own search ("me"), an administrator's search of one user's workspace
("{user_id}") and of the organisation ("all"). Listed in tests/authz_matrix.py.

The query is never logged, audited or stored: the access log records the path without the
query string (core.middleware), and search writes nothing (search.selectors). Each user may
search 120 times a minute (a scoped throttle on top of the global per-user limit); a refused
search is answered before the workspace is resolved or anything is searched. A search the
database gives up on is a 503 "search_busy" (docs/search.md#resource-protection).
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle

from arkray.core.api import ApiView, validated
from arkray.core.errors import PermissionDeniedError, error_body
from arkray.core.ranking import Matches
from arkray.identity.models import User
from arkray.identity.permissions import IsActiveUser
from arkray.identity.workspaces import resolve_workspace
from arkray.leads.api.views import NOT_FOUND

from .. import selectors
from . import serializers as s

# Longer than the web app retries by itself (4 s, lib/query-client.ts): a search that just
# timed out isn't sent again automatically; the person chooses to try again.
BUSY_RETRY_AFTER_S = 5


def _group(matches: Matches[Any]) -> dict[str, Any]:
    return {"results": matches.items, "has_more": matches.has_more}


class SearchView(ApiView):
    """Leads, opportunities, tasks, meetings and notes in this workspace that match `q`."""

    permission_classes = [IsActiveUser]
    query_param_methods = frozenset({"GET"})
    # The global per-user limit, plus search's own (settings: API_THROTTLE_SEARCH). Both
    # live in the cache: with Redis down they don't apply, and search still answers.
    throttle_classes = [UserRateThrottle, ScopedRateThrottle]
    throttle_scope = "search"

    @extend_schema(
        operation_id="search",
        parameters=[s.SearchQuerySerializer],
        responses={
            200: s.SearchResultsSerializer,
            404: NOT_FOUND,
            429: OpenApiResponse(description="Too many searches; see Retry-After."),
            503: OpenApiResponse(
                description="search_busy: the search took too long; try again (Retry-After)."
            ),
        },
    )
    def get(self, request: Request, workspace: str) -> Response:
        actor = request.user
        if not isinstance(actor, User):  # unreachable behind the permission classes
            raise PermissionDeniedError()
        scope = resolve_workspace(actor, workspace)
        query = validated(s.SearchQuerySerializer, request.query_params)["q"]
        try:
            found = selectors.global_search(scope, query)
        except selectors.SearchBusy as busy:
            response = Response(error_body(busy.code, busy.message), status=busy.http_status)
            response["Retry-After"] = str(BUSY_RETRY_AFTER_S)
            return response
        data = {
            "query": found.query.text,
            "terms": list(found.query.terms),
            "leads": _group(found.leads),
            "opportunities": _group(found.opportunities),
            "tasks": _group(found.tasks),
            "meetings": _group(found.meetings),
            "notes": _group(found.notes),
        }
        # Related leads render only if visible in this scope; "overdue" at one `now`.
        context = {"scope": scope, "now": timezone.now()}
        return Response(s.SearchResultsSerializer(data, context=context).data)
