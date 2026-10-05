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
from urllib.parse import quote
from uuid import UUID

from django.http import StreamingHttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
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

from .. import attachments, selectors, services, storage
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


# --- notes on a deal (the deal page's Notes) ------------------------------------------------------
class OpportunityNotesView(ApiView):
    """An opportunity's notes, newest first: whole text, author, edits, files and whether
    the caller may change each one. Adding a note is POST /activities {type: "note",
    opportunity}; editing it PATCH /activities/{id}."""

    permission_classes = [IsActiveUser]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="opportunities_notes",
        parameters=[s.NoteQuerySerializer],
        responses={200: s.NotePageSerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str, opportunity_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        params = validated(s.NoteQuerySerializer, request.query_params)
        paginator = KeysetPaginator(
            selectors.NOTE_ORDERING,
            page_size=params["page_size"],
            binding=CursorBinding.of(
                f"opportunities.notes:{opportunity_id}",
                params,
                actor_id=scope.actor_id,
                scope=scope,
            ),
        )
        page = paginator.paginate(
            selectors.opportunity_notes(scope, opportunity_id), params.get("cursor")
        )
        try:
            authorize_write(actor, scope)
            writable = True
        except PermissionDeniedError:
            writable = False
        context = {
            "scope": scope,
            "actor": actor,
            "writable": writable,
            "attachments": attachments.for_notes([note.pk for note in page.items]),
        }
        return Response(
            {
                "results": s.NoteSerializer(page.items, many=True, context=context).data,
                **page_links(request, page),
            }
        )


# --- attachments ----------------------------------------------------------------------------------
FILENAME_HEADER = "X-Filename"


class NoteAttachmentsView(ApiView):
    """Attach a file to a note. The body is the file itself (Content-Type:
    application/octet-stream) and the X-Filename header its name, percent-encoded. At most
    ATTACHMENT_MAX_BYTES; allowed types only, recognised by their content."""

    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="activities_attachments_upload",
        request={"application/octet-stream": OpenApiTypes.BINARY},
        parameters=[
            OpenApiParameter(
                FILENAME_HEADER,
                OpenApiTypes.STR,
                OpenApiParameter.HEADER,
                required=True,
                description="The file's name, percent-encoded (UTF-8).",
            )
        ],
        responses={201: s.AttachmentSerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str, activity_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        filename = request.headers.get(FILENAME_HEADER, "")
        try:
            declared = int(request.headers.get("Content-Length", ""))
        except ValueError:
            declared = None
        # The raw stream: never request.data (no parser runs, nothing is buffered whole).
        attachment = attachments.upload(
            actor=actor,
            scope=scope,
            note_id=activity_id,
            filename=filename,
            stream=request._request,
            declared_length=declared,
        )
        return Response(s.AttachmentSerializer(attachment).data, status=http.HTTP_201_CREATED)


class AttachmentView(ApiView):
    permission_classes = [IsActiveUser]

    @extend_schema(operation_id="attachments_delete", request=None, responses={204: None})
    def delete(self, request: Request, workspace: str, attachment_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        attachments.delete(actor=actor, scope=scope, attachment_id=attachment_id)
        return Response(status=http.HTTP_204_NO_CONTENT)


# What a file response may do in a browser: nothing. No scripts, no plugins, no framing, its
# own sandboxed origin even if opened directly; never sniffed into another type.
FILE_CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"


def _file_response(download: attachments.Download, *, inline: bool) -> StreamingHttpResponse:
    attachment = download.attachment
    response = StreamingHttpResponse(
        storage.iter_file(download.file), content_type=attachment.content_type
    )
    disposition = "inline" if inline else "attachment"
    fallback = storage.ascii_fallback(attachment.original_name)
    response["Content-Disposition"] = (
        f'{disposition}; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(attachment.original_name, safe='')}"
    )
    response["Content-Length"] = str(attachment.size)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = FILE_CSP
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    response["Cache-Control"] = "private, no-store"
    return response


class AttachmentDownloadView(ApiView):
    """The file, as a download, if its note is visible in this workspace now (re-checked on
    every request) and the virus scan allows it."""

    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="attachments_download",
        responses={(200, "application/octet-stream"): OpenApiTypes.BINARY, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str, attachment_id: UUID) -> StreamingHttpResponse:
        _, scope = _scope(request, workspace)
        download = attachments.open_for_download(scope=scope, attachment_id=attachment_id)
        return _file_response(download, inline=False)


class AttachmentPreviewView(ApiView):
    """An image file, shown inline (PNG, JPEG, WebP, GIF only; validated at upload)."""

    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="attachments_preview",
        responses={(200, "image/*"): OpenApiTypes.BINARY, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str, attachment_id: UUID) -> StreamingHttpResponse:
        _, scope = _scope(request, workspace)
        download = attachments.open_for_download(
            scope=scope, attachment_id=attachment_id, preview=True
        )
        return _file_response(download, inline=True)
