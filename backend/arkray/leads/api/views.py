"""Lead API views: thin HTTP adapters over leads.selectors and leads.services.

Every route is nested under /api/v1/workspaces/{workspace}/ and starts by resolving the
workspace into an AccessScope (404 for workspaces the caller may not open; delegated access
is audited). The same views serve a salesperson's own leads ("me"), one user's leads opened
by an administrator ("{user_id}") and the organisation ("all"). Every route is listed in
tests/authz_matrix.py.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status as http
from rest_framework.permissions import SAFE_METHODS
from rest_framework.request import Request
from rest_framework.response import Response

from arkray.core.access import AccessScope
from arkray.core.api import IDEMPOTENCY_HEADER, ApiView, idempotency_key, validated
from arkray.core.errors import InvalidInputError, PermissionDeniedError
from arkray.core.keyset import CursorBinding, KeysetPaginator, page_links
from arkray.identity.models import User
from arkray.identity.permissions import IsActiveUser, requires
from arkray.identity.policy import Capability
from arkray.identity.workspaces import authorize_write, resolve_workspace, workspace_segment

from .. import selectors, services
from ..countries import COUNTRY_CODES
from ..models import Rating
from . import serializers as s

OWNER_FILTER_ORG_ONLY = "Filtering by owner is only available in the organization-wide workspace."

IDEMPOTENCY_PARAMETER = OpenApiParameter(
    IDEMPOTENCY_HEADER,
    OpenApiTypes.UUID,
    location=OpenApiParameter.HEADER,
    required=False,
    description="A UUID chosen by the client. Repeating the same request with the same key "
    "within 24 hours returns the lead created the first time instead of a duplicate.",
)
NOT_FOUND = OpenApiResponse(description="Not found, or outside this workspace.")


def _actor(request: Request) -> User:
    user = request.user
    if not isinstance(user, User):  # unreachable behind the permission classes
        raise PermissionDeniedError()
    return user


def _scope(request: Request, workspace: str) -> tuple[User, AccessScope]:
    actor = _actor(request)
    scope = resolve_workspace(actor, workspace)
    # Unsafe methods are writes: refused before the body is even read (Phase 9: a viewer's
    # write with a malformed body got a validation error instead of a 403).
    if request.method not in SAFE_METHODS:
        authorize_write(actor, scope)
    return actor, scope


def _lead(lead: Any) -> dict[str, Any]:
    return dict(s.LeadSerializer(lead).data)


class LeadListView(ApiView):
    permission_classes = [IsActiveUser]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="leads_list",
        parameters=[s.LeadListQuerySerializer],
        responses={200: s.LeadPageSerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str) -> Response:
        _, scope = _scope(request, workspace)
        params = validated(s.LeadListQuerySerializer, request.query_params)
        if "owner" in params and not scope.is_organization_wide:
            raise InvalidInputError(details={"owner": [OWNER_FILTER_ORG_ONLY]})
        leads = selectors.lead_list(
            scope,
            selectors.LeadFilters(
                q=params.get("q", ""),
                status=params.get("status"),
                source=params.get("source"),
                rating=params.get("rating"),
                owner_id=params.get("owner"),
                created_from=params.get("created_from"),
                created_to=params.get("created_to"),
                archived=params["archived"],
            ),
        )
        paginator = KeysetPaginator(
            selectors.ORDERINGS[params["ordering"]],
            page_size=params["page_size"],
            binding=CursorBinding.of("leads.list", params, actor_id=scope.actor_id, scope=scope),
        )
        page = paginator.paginate(leads, params.get("cursor"), visible=selectors.listable(scope))
        return Response(
            {
                "results": s.LeadListItemSerializer(page.items, many=True).data,
                **page_links(request, page),
            }
        )

    @extend_schema(
        operation_id="leads_create",
        request=s.LeadCreateSerializer,
        parameters=[IDEMPOTENCY_PARAMETER],
        responses={201: s.LeadSerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.LeadCreateSerializer, request.data)
        status_key = data.pop("status", None)
        owner = data.pop("owner", None)
        result = services.create_lead(
            actor=actor,
            scope=scope,
            fields=data,
            status=status_key,
            owner_id=owner,
            idempotency_key=idempotency_key(request),
        )
        response = Response(_lead(result.lead), status=http.HTTP_201_CREATED)
        response["Location"] = (
            f"/api/v1/workspaces/{workspace_segment(scope)}/leads/{result.lead.pk}"
        )
        if result.replayed:
            response["Idempotent-Replayed"] = "true"
        return response


class LeadDuplicatesView(ApiView):
    """Possible duplicates of the contact details being entered, within this workspace only
    (never a way to learn about leads the caller can't see). Advisory: nothing is blocked
    or merged."""

    permission_classes = [IsActiveUser]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="leads_duplicates",
        parameters=[s.LeadDuplicateQuerySerializer],
        responses={200: s.LeadDuplicateListSerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str) -> Response:
        _, scope = _scope(request, workspace)
        params = validated(s.LeadDuplicateQuerySerializer, request.query_params)
        found = selectors.possible_duplicates(
            scope,
            email=params.get("email", ""),
            phones=params.get("phone", []),
            exclude_id=params.get("exclude"),
        )
        context = {"matched_on": {lead.pk: matched for lead, matched in found}}
        leads = [lead for lead, _ in found]
        return Response(
            {"results": s.LeadDuplicateSerializer(leads, many=True, context=context).data}
        )


class LeadDetailView(ApiView):
    permission_classes = [IsActiveUser]

    @extend_schema(operation_id="leads_retrieve", responses={200: s.LeadSerializer, 404: NOT_FOUND})
    def get(self, request: Request, workspace: str, lead_id: UUID) -> Response:
        _, scope = _scope(request, workspace)
        return Response(_lead(selectors.lead_detail(scope, lead_id)))

    @extend_schema(
        operation_id="leads_update",
        request=s.LeadUpdateSerializer,
        responses={200: s.LeadSerializer, 404: NOT_FOUND},
    )
    def patch(self, request: Request, workspace: str, lead_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.LeadUpdateSerializer, request.data)
        version = data.pop("version")
        lead = services.update_lead(
            actor=actor, scope=scope, lead_id=lead_id, version=version, changes=data
        )
        return Response(_lead(lead))


class LeadStatusView(ApiView):
    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="leads_change_status",
        request=s.LeadStatusChangeSerializer,
        responses={200: s.LeadSerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str, lead_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.LeadStatusChangeSerializer, request.data)
        lead = services.change_status(
            actor=actor,
            scope=scope,
            lead_id=lead_id,
            version=data["version"],
            status=data["status"],
        )
        return Response(_lead(lead))


class LeadAssignView(ApiView):
    """Reassign a lead (crm.assign_any). The response shows the lead as reassigned, even if
    it has thereby left the workspace it was reassigned from."""

    permission_classes = [requires(Capability.CRM_ASSIGN_ANY)]

    @extend_schema(
        operation_id="leads_assign",
        request=s.LeadAssignSerializer,
        responses={200: s.LeadSerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str, lead_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.LeadAssignSerializer, request.data)
        lead = services.reassign_lead(
            actor=actor,
            scope=scope,
            lead_id=lead_id,
            version=data["version"],
            owner_id=data["owner"],
        )
        return Response(_lead(lead))


class LeadArchiveView(ApiView):
    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="leads_archive",
        request=s.LeadVersionSerializer,
        responses={200: s.LeadSerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str, lead_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.LeadVersionSerializer, request.data)
        lead = services.archive_lead(
            actor=actor, scope=scope, lead_id=lead_id, version=data["version"]
        )
        return Response(_lead(lead))


class LeadRestoreView(ApiView):
    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="leads_restore",
        request=s.LeadVersionSerializer,
        responses={200: s.LeadSerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str, lead_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.LeadVersionSerializer, request.data)
        lead = services.restore_lead(
            actor=actor, scope=scope, lead_id=lead_id, version=data["version"]
        )
        return Response(_lead(lead))


class LeadOptionsView(ApiView):
    """Statuses, sources, ratings and country codes for lead forms and filters."""

    permission_classes = [IsActiveUser]

    @extend_schema(operation_id="lead_options", responses={200: s.LeadOptionsSerializer})
    def get(self, request: Request) -> Response:
        return Response(
            s.LeadOptionsSerializer(
                {
                    "statuses": selectors.statuses(),
                    "sources": selectors.sources(),
                    "ratings": [{"key": r.value, "name": r.label} for r in Rating],
                    "countries": sorted(COUNTRY_CODES),
                }
            ).data
        )
