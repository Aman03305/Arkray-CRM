"""Pipeline API views: thin HTTP adapters over pipeline.selectors and pipeline.services.

Every CRM route is nested under /api/v1/workspaces/{workspace}/ and starts by resolving the
workspace into an AccessScope (404 for workspaces the caller may not open; delegated access
is audited). The same views serve a salesperson's own pipeline ("me"), one user's pipeline
opened by an administrator ("{user_id}") and the organisation ("all"). Every route is
listed in tests/authz_matrix.py.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status as http
from rest_framework.permissions import SAFE_METHODS
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.utils.urls import replace_query_param

from arkray.core.access import AccessScope
from arkray.core.api import ApiView, idempotency_key, validated
from arkray.core.errors import InvalidInputError, PermissionDeniedError
from arkray.core.keyset import CursorBinding, KeysetPaginator, page_links
from arkray.identity.models import User
from arkray.identity.permissions import IsActiveUser
from arkray.identity.workspaces import authorize_write, resolve_workspace, workspace_segment
from arkray.leads.api.views import IDEMPOTENCY_PARAMETER, NOT_FOUND, OWNER_FILTER_ORG_ONLY

from .. import selectors, services
from ..selectors import OpportunityFilters
from . import serializers as s

CREATE_IDEMPOTENCY = IDEMPOTENCY_PARAMETER


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


def _filters(params: dict[str, Any], scope: AccessScope, **extra: Any) -> OpportunityFilters:
    if "owner" in params and not scope.is_organization_wide:
        raise InvalidInputError(details={"owner": [OWNER_FILTER_ORG_ONLY]})
    return OpportunityFilters(
        lead_id=params.get("lead"),
        owner_id=params.get("owner"),
        expected_close_from=params.get("expected_close_from"),
        expected_close_to=params.get("expected_close_to"),
        probability_min=params.get("probability_min"),
        probability_max=params.get("probability_max"),
        **extra,
    )


def _opportunity(opportunity: Any, scope: AccessScope) -> dict[str, Any]:
    return dict(s.OpportunitySerializer(opportunity, context={"scope": scope}).data)


def _location(scope: AccessScope, opportunity_id: UUID) -> str:
    return f"/api/v1/workspaces/{workspace_segment(scope)}/opportunities/{opportunity_id}"


class PipelineConfigView(ApiView):
    """Pipelines and their stages (configuration, the same for everyone)."""

    permission_classes = [IsActiveUser]

    @extend_schema(operation_id="pipelines_list", responses={200: s.PipelineListSerializer})
    def get(self, request: Request) -> Response:
        return Response(s.PipelineListSerializer({"results": selectors.pipelines()}).data)


# The opportunities list's cursors, also issued by the board for its columns' `next`.
OPPORTUNITY_LIST = "opportunities.list"


class BoardView(ApiView):
    """The Kanban board: every stage of one pipeline with its count, value, weighted value
    and at most `cards_per_stage` cards, plus the open-pipeline totals. Bounded whatever
    the number of opportunities; a column's `next` continues in the opportunities list."""

    permission_classes = [IsActiveUser]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="pipeline_board",
        parameters=[s.BoardQuerySerializer],
        responses={200: s.BoardSerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str) -> Response:
        _, scope = _scope(request, workspace)
        params = validated(s.BoardQuerySerializer, request.query_params)
        filters = _filters(params, scope)
        pipeline = selectors.pipeline_for_board(params.get("pipeline"))

        def continuation(stage_id: UUID, ordering: str) -> dict[str, str]:
            """The opportunities list request that continues a column."""
            query = {**request.query_params.dict(), "pipeline": str(pipeline.pk)}
            query.pop("cards_per_stage", None)
            query.update(
                stage=str(stage_id),
                ordering=ordering,
                page_size=str(params["cards_per_stage"] or selectors.BOARD_CARDS_DEFAULT),
            )
            return query

        def binding_for(stage_id: UUID, ordering: str) -> CursorBinding:
            # Bound exactly as the opportunities list will check it (same validated filters).
            list_params = validated(
                s.OpportunityListQuerySerializer, continuation(stage_id, ordering)
            )
            return CursorBinding.of(
                OPPORTUNITY_LIST, list_params, actor_id=scope.actor_id, scope=scope
            )

        board = selectors.board(
            scope,
            pipeline,
            filters,
            cards_per_stage=params["cards_per_stage"],
            binding_for=binding_for,
        )
        context = {"scope": scope}
        list_url = request.build_absolute_uri(
            f"/api/v1/workspaces/{workspace_segment(scope)}/opportunities"
        )
        columns = []
        for column in board.columns:
            next_url = None
            # More in this stage than the cards shown: the list continues after the last
            # card (or starts at the stage's first page when no cards were asked for).
            if column.page.next_cursor or column.count > len(column.page.items):
                query = continuation(column.stage.pk, column.ordering)
                if column.page.next_cursor:
                    query["cursor"] = column.page.next_cursor
                next_url = list_url
                for key, value in query.items():
                    next_url = replace_query_param(next_url, key, value)
            columns.append(
                {
                    "stage": column.stage,
                    "count": column.count,
                    "total_value": column.total_value,
                    "weighted_value": column.weighted_value,
                    "ordering": column.ordering,
                    "cards": column.page.items,
                    "next": next_url,
                }
            )
        data = s.BoardSerializer(
            {
                "pipeline": pipeline,
                "currency": settings.CRM_CURRENCY,
                "totals": board.totals,
                "columns": columns,
            },
            context=context,
        ).data
        return Response(data)


class PipelineSummaryView(ApiView):
    """Pipeline value, weighted pipeline and the open count in this workspace: the
    authoritative figures (the dashboard shows exactly these)."""

    permission_classes = [IsActiveUser]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="pipeline_summary",
        parameters=[s.SummaryQuerySerializer],
        responses={200: s.PipelineSummarySerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str) -> Response:
        _, scope = _scope(request, workspace)
        params = validated(s.SummaryQuerySerializer, request.query_params)
        totals = selectors.pipeline_totals(
            scope, _filters(params, scope, pipeline_id=params.get("pipeline"))
        )
        return Response(
            s.PipelineSummarySerializer({"currency": settings.CRM_CURRENCY, "totals": totals}).data
        )


class OpportunityListView(ApiView):
    permission_classes = [IsActiveUser]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="opportunities_list",
        parameters=[s.OpportunityListQuerySerializer],
        responses={200: s.OpportunityPageSerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str) -> Response:
        _, scope = _scope(request, workspace)
        params = validated(s.OpportunityListQuerySerializer, request.query_params)
        filters = _filters(
            params,
            scope,
            pipeline_id=params.get("pipeline"),
            stage_id=params.get("stage"),
            status=params.get("status"),
            archived=params["archived"],
        )
        paginator = KeysetPaginator(
            selectors.ORDERINGS[params["ordering"]],
            page_size=params["page_size"],
            binding=CursorBinding.of(
                OPPORTUNITY_LIST, params, actor_id=scope.actor_id, scope=scope
            ),
        )
        page = paginator.paginate(
            selectors.opportunity_list(scope, filters),
            params.get("cursor"),
            visible=selectors.listable(scope),
        )
        return Response(
            {
                "results": s.OpportunityCardSerializer(
                    page.items, many=True, context={"scope": scope}
                ).data,
                **page_links(request, page),
            }
        )

    @extend_schema(
        operation_id="opportunities_create",
        request=s.OpportunityCreateSerializer,
        parameters=[CREATE_IDEMPOTENCY],
        responses={201: s.OpportunitySerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.OpportunityCreateSerializer, request.data)
        lead_id = data.pop("lead")
        pipeline_id = data.pop("pipeline", None)
        stage_id = data.pop("stage", None)
        result = services.create_opportunity(
            actor=actor,
            scope=scope,
            lead_id=lead_id,
            fields=data,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            idempotency_key=idempotency_key(request),
        )
        response = Response(_opportunity(result.opportunity, scope), status=http.HTTP_201_CREATED)
        response["Location"] = _location(scope, result.opportunity.pk)
        if result.replayed:
            response["Idempotent-Replayed"] = "true"
        return response


class OpportunityDetailView(ApiView):
    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="opportunities_retrieve",
        responses={200: s.OpportunitySerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str, opportunity_id: UUID) -> Response:
        _, scope = _scope(request, workspace)
        return Response(_opportunity(selectors.opportunity_detail(scope, opportunity_id), scope))

    @extend_schema(
        operation_id="opportunities_update",
        request=s.OpportunityUpdateSerializer,
        responses={200: s.OpportunitySerializer, 404: NOT_FOUND},
    )
    def patch(self, request: Request, workspace: str, opportunity_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.OpportunityUpdateSerializer, request.data)
        version = data.pop("version")
        opportunity = services.update_opportunity(
            actor=actor, scope=scope, opportunity_id=opportunity_id, version=version, changes=data
        )
        return Response(_opportunity(opportunity, scope))


class OpportunityMoveView(ApiView):
    """The one stage-transition operation (drag and drop and "Move to stage" alike):
    open stages, won, lost, and reopening a closed opportunity into an open stage."""

    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="opportunities_move",
        request=s.OpportunityMoveSerializer,
        responses={200: s.OpportunitySerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str, opportunity_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.OpportunityMoveSerializer, request.data)
        opportunity = services.move_opportunity(
            actor=actor,
            scope=scope,
            opportunity_id=opportunity_id,
            version=data["version"],
            stage_id=data["stage"],
            lost_reason=data.get("lost_reason", ""),
        )
        return Response(_opportunity(opportunity, scope))


class OpportunityArchiveView(ApiView):
    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="opportunities_archive",
        request=s.OpportunityVersionSerializer,
        responses={200: s.OpportunitySerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str, opportunity_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.OpportunityVersionSerializer, request.data)
        opportunity = services.archive_opportunity(
            actor=actor, scope=scope, opportunity_id=opportunity_id, version=data["version"]
        )
        return Response(_opportunity(opportunity, scope))


class OpportunityRestoreView(ApiView):
    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="opportunities_restore",
        request=s.OpportunityVersionSerializer,
        responses={200: s.OpportunitySerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str, opportunity_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.OpportunityVersionSerializer, request.data)
        opportunity = services.restore_opportunity(
            actor=actor, scope=scope, opportunity_id=opportunity_id, version=data["version"]
        )
        return Response(_opportunity(opportunity, scope))


class OpportunityHistoryView(ApiView):
    """Stage history, newest first (append-only; never editable)."""

    permission_classes = [IsActiveUser]
    query_param_methods = frozenset({"GET"})

    @extend_schema(
        operation_id="opportunities_history",
        parameters=[s.HistoryQuerySerializer],
        responses={200: s.StageHistoryPageSerializer, 404: NOT_FOUND},
    )
    def get(self, request: Request, workspace: str, opportunity_id: UUID) -> Response:
        _, scope = _scope(request, workspace)
        params = validated(s.HistoryQuerySerializer, request.query_params)
        paginator = KeysetPaginator(
            selectors.HISTORY_ORDERING,
            page_size=params["page_size"],
            binding=CursorBinding.of(
                f"opportunities.history:{opportunity_id}",
                params,
                actor_id=scope.actor_id,
                scope=scope,
            ),
        )
        page = paginator.paginate(
            selectors.stage_history(scope, opportunity_id), params.get("cursor")
        )
        return Response(
            {
                "results": s.StageHistorySerializer(page.items, many=True).data,
                **page_links(request, page),
            }
        )


class LeadConvertView(ApiView):
    """Convert a lead: create its opportunity and set the lead's status to Converted, in
    one transaction. Send an Idempotency-Key so a retried request can't convert twice."""

    permission_classes = [IsActiveUser]

    @extend_schema(
        operation_id="leads_convert",
        request=s.LeadConvertSerializer,
        parameters=[CREATE_IDEMPOTENCY],
        responses={201: s.ConversionSerializer, 404: NOT_FOUND},
    )
    def post(self, request: Request, workspace: str, lead_id: UUID) -> Response:
        actor, scope = _scope(request, workspace)
        data = validated(s.LeadConvertSerializer, request.data)
        version = data.pop("version")
        pipeline_id = data.pop("pipeline", None)
        stage_id = data.pop("stage", None)
        result = services.convert_lead(
            actor=actor,
            scope=scope,
            lead_id=lead_id,
            lead_version=version,
            fields=data,
            pipeline_id=pipeline_id,
            stage_id=stage_id,
            idempotency_key=idempotency_key(request),
        )
        body = s.ConversionSerializer(
            {"lead": result.lead, "opportunity": result.opportunity}, context={"scope": scope}
        ).data
        response = Response(body, status=http.HTTP_201_CREATED)
        response["Location"] = _location(scope, result.opportunity.pk)
        if result.replayed:
            response["Idempotent-Replayed"] = "true"
        return response
