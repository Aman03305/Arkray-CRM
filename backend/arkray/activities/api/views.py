"""Activity and timeline API views: thin HTTP adapters over activities.selectors and
activities.services.

Every route is nested under /api/v1/workspaces/{workspace}/ and starts by resolving the
workspace into an AccessScope (404 for workspaces the caller may not open; delegated
access is audited). The same views serve a salesperson's own activities ("me"), one user's
opened by an administrator ("{user_id}") and the organisation ("all"). Lifecycle changes
are explicit actions (complete, cancel, reopen, archive, restore), never a generic PATCH.
Every route is listed in tests/authz_matrix.py.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar
from uuid import UUID

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status as http
from rest_framework.permissions import SAFE_METHODS
from rest_framework.request import Request
from rest_framework.response import Response

from arkray.core.access import AccessScope
from arkray.core.api import ApiView, idempotency_key, validated
from arkray.core.errors import InvalidInputError, PermissionDeniedError
from arkray.core.keyset import CursorBinding, KeysetPaginator, page_links
from arkray.identity.models import User
from arkray.identity.permissions import IsActiveUser
from arkray.identity.workspaces import authorize_write, resolve_workspace, workspace_segment
from arkray.leads.api.views import IDEMPOTENCY_PARAMETER, NOT_FOUND, OWNER_FILTER_ORG_ONLY

from .. import selectors, services
from ..models import Activity, TimelineEntry
from ..selectors import ActivityFilters
from . import serializers as s

_View = TypeVar("_View", bound=Callable[..., Response])


def _scope(request: Request, workspace: str) -> tuple[User, AccessScope]:
    actor = request.user
    if not isinstance(actor, User):  # unreachable behind the permission classes
        raise PermissionDeniedError()
    scope = resolve_workspace(actor, workspace)
    # Unsafe methods are writes: refused before the body is even read (Phase 9: a viewer's
    # write with a malformed body got a validation error instead of a 403).
    if request.method not in SAFE_METHODS:
        authorize_write(actor, scope)
    return actor, scope


def _context(scope: AccessScope) -> dict[str, Any]:
    return {"scope": scope, "now": timezone.now()}


def _activity(activity: Activity, scope: AccessScope) -> dict[str, Any]:
    return dict(s.ActivitySerializer(activity, context=_context(scope)).data)


class ActivityListView(ApiView):
    permission_classes = [IsActiveUser]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="activities_list",
        parameters=[s.ActivityListQuerySerializer],
        responses={200: s.ActivityPageSerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str) -> Response:
        _, scope = _scope(request, workspace)
        params = validated(s.ActivityListQuerySerializer, request.query_params)
        if "owner" in params and not scope.is_organization_wide:
            raise InvalidInputError(details={"owner": [OWNER_FILTER_ORG_ONLY]})
        context = _context(scope)
        filters = ActivityFilters(
            type=params.get("type"),
            status=params.get("status"),
            lead_id=params.get("lead"),
            opportunity_id=params.get("opportunity"),
            owner_id=params.get("owner"),
            date_from=params.get("date_from"),
            date_to=params.get("date_to"),
            overdue=params["overdue"],
            current=params["current"],
            upcoming=params["upcoming"],
            cancelled=params["cancelled"],
            archived=params["archived"],
        )
        paginator = KeysetPaginator(
            selectors.ordering(params["ordering"], scope, filters),
            page_size=params["page_size"],
            binding=CursorBinding.of(
                "activities.list", params, actor_id=scope.actor_id, scope=scope
            ),
        )
        page = paginator.paginate(
            selectors.activity_list(scope, filters, now=context["now"]), params.get("cursor")
        )
        return Response(
            {
                "results": s.ActivityListItemSerializer(
                    page.items, many=True, context=context
                ).data,
                **page_links(request, page),
            }
        )

    @extend_schema(
        operation_id="activities_create",
        request=s.ActivityCreateSerializer,
        parameters=[IDEMPOTENCY_PARAMETER],
        responses={201: s.ActivitySerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.ActivityCreateSerializer, request.data)
        activity_type = data.pop("type")
        lead_id = data.pop("lead", None)
        opportunity_id = data.pop("opportunity", None)
        result = services.create_activity(
            actor=actor,
            scope=scope,
            activity_type=activity_type,
            lead_id=lead_id,
            opportunity_id=opportunity_id,
            fields=data,
            idempotency_key=idempotency_key(request),
        )
        response = Response(_activity(result.activity, scope), status=http.HTTP_201_CREATED)
        response["Location"] = (
            f"/api/v1/workspaces/{workspace_segment(scope)}/activities/{result.activity.pk}"
        )
        if result.replayed:
            response["Idempotent-Replayed"] = "true"
        return response


class ActivityDetailView(ApiView):
    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="activities_retrieve", responses={200: s.ActivitySerializer, 404: NOT_FOUND}
    )
    def get(self, request: Request, workspace: str, activity_id: UUID) -> Response:
        _, scope = _scope(request, workspace)
        return Response(_activity(selectors.activity_detail(scope, activity_id), scope))

    @extend_schema(
        operation_id="activities_update",
        request=s.ActivityUpdateSerializer,
        responses={200: s.ActivitySerializer, 404: NOT_FOUND},
    )
    def patch(self, request: Request, workspace: str, activity_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.ActivityUpdateSerializer, request.data)
        version = data.pop("version")
        activity = services.update_activity(
            actor=actor, scope=scope, activity_id=activity_id, version=version, changes=data
        )
        return Response(_activity(activity, scope))


def _act(
    request: Request, workspace: str, activity_id: UUID, operation: Callable[..., Activity]
) -> Response:
    """POST {version}: one lifecycle operation. Repeating a change that already happened
    (completing a completed task) succeeds without changing anything."""
    actor, scope = _scope(request, workspace)
    data = validated(s.ActivityVersionSerializer, request.data)
    activity = operation(actor=actor, scope=scope, activity_id=activity_id, version=data["version"])
    return Response(_activity(activity, scope))


def _action_schema(action: str, summary: str) -> Callable[[_View], _View]:
    return extend_schema(
        operation_id=f"activities_{action}",
        summary=summary,
        request=s.ActivityVersionSerializer,
        responses={200: s.ActivitySerializer, 404: NOT_FOUND},
    )


class ActivityCompleteView(ApiView):
    permission_classes = [IsActiveUser]

    @_action_schema(
        "complete", "Complete a task, or record that a meeting took place (updates last contact)."
    )
    def post(self, request: Request, workspace: str, activity_id: UUID) -> Response:
        return _act(request, workspace, activity_id, services.complete_activity)


class ActivityCancelView(ApiView):
    permission_classes = [IsActiveUser]

    @_action_schema("cancel", "Cancel a task or a meeting.")
    def post(self, request: Request, workspace: str, activity_id: UUID) -> Response:
        return _act(request, workspace, activity_id, services.cancel_activity)


class ActivityReopenView(ApiView):
    permission_classes = [IsActiveUser]

    @_action_schema(
        "reopen", "Reopen a completed or cancelled task or meeting (it follows the lead's owner)."
    )
    def post(self, request: Request, workspace: str, activity_id: UUID) -> Response:
        return _act(request, workspace, activity_id, services.reopen_activity)


class ActivityArchiveView(ApiView):
    permission_classes = [IsActiveUser]

    @_action_schema("archive", "Hide an activity from lists, timelines and counts.")
    def post(self, request: Request, workspace: str, activity_id: UUID) -> Response:
        return _act(request, workspace, activity_id, services.archive_activity)


class ActivityRestoreView(ApiView):
    permission_classes = [IsActiveUser]

    @_action_schema("restore", "Bring an archived activity back.")
    def post(self, request: Request, workspace: str, activity_id: UUID) -> Response:
        return _act(request, workspace, activity_id, services.restore_activity)


class ActivitySummaryView(ApiView):
    """Open tasks, tasks due today, overdue tasks, today's meetings and upcoming meetings in
    this workspace: the authoritative figures (the dashboard will show exactly these)."""

    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="activity_summary",
        responses={200: s.ActivitySummarySerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str) -> Response:
        _, scope = _scope(request, workspace)
        summary = selectors.activity_summary(scope, now=timezone.now())
        return Response(s.ActivitySummarySerializer(summary).data)


def _timeline_page(
    request: Request, scope: AccessScope, entries: Any, *, purpose: str
) -> dict[str, Any]:
    params = validated(s.TimelineQuerySerializer, request.query_params)
    paginator = KeysetPaginator(
        selectors.TIMELINE_ORDERING,
        page_size=params["page_size"],
        binding=CursorBinding.of(purpose, params, actor_id=scope.actor_id, scope=scope),
    )
    page = paginator.paginate(entries, params.get("cursor"))
    rows: list[TimelineEntry] = page.items
    context = {"scope": scope, "people": selectors.people_in(rows)}
    return {
        "results": s.TimelineEntrySerializer(rows, many=True, context=context).data,
        **page_links(request, page),
    }


class LeadTimelineView(ApiView):
    """A lead's history, newest first: lead events, its opportunities' events and its
    activities, each only while visible in this workspace."""

    permission_classes = [IsActiveUser]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="leads_timeline",
        parameters=[s.TimelineQuerySerializer],
        responses={200: s.TimelinePageSerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str, lead_id: UUID) -> Response:
        _, scope = _scope(request, workspace)
        return Response(
            _timeline_page(
                request,
                scope,
                selectors.lead_timeline(scope, lead_id),
                purpose=f"leads.timeline:{lead_id}",
            )
        )


class OpportunityTimelineView(ApiView):
    """An opportunity's history and its activities, newest first."""

    permission_classes = [IsActiveUser]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="opportunities_timeline",
        parameters=[s.TimelineQuerySerializer],
        responses={200: s.TimelinePageSerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str, opportunity_id: UUID) -> Response:
        _, scope = _scope(request, workspace)
        return Response(
            _timeline_page(
                request,
                scope,
                selectors.opportunity_timeline(scope, opportunity_id),
                purpose=f"opportunities.timeline:{opportunity_id}",
            )
        )
