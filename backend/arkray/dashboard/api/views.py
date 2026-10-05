"""Dashboard API view: a thin HTTP adapter over dashboard.selectors.

Nested under /api/v1/workspaces/{workspace}/ like every CRM route: the workspace resolves
to an AccessScope first (404 for workspaces the caller may not open; delegated access is
audited by resolve_workspace, once per window, not per refresh), and every figure is
computed inside that scope. The same view serves a salesperson's own dashboard ("me"), one
user's opened by an administrator ("{user_id}") and the Admin Home ("all"). Listed in
tests/authz_matrix.py.
"""

from __future__ import annotations

from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from arkray.core.api import ApiView
from arkray.core.errors import PermissionDeniedError
from arkray.identity.models import User
from arkray.identity.permissions import IsActiveUser
from arkray.identity.workspaces import resolve_workspace
from arkray.leads.api.views import NOT_FOUND

from .. import selectors
from . import serializers as s


class DashboardView(ApiView):
    """Total leads, new leads today, pipeline value, weighted pipeline, meetings and tasks in
    this workspace, with today's newest leads, the next meetings and the next open tasks."""

    permission_classes = [IsActiveUser]

    @extend_schema(operation_id="dashboard", responses={200: s.DashboardSerializer, 404: NOT_FOUND})
    def get(self, request: Request, workspace: str) -> Response:
        actor = request.user
        if not isinstance(actor, User):  # unreachable behind the permission classes
            raise PermissionDeniedError()
        scope = resolve_workspace(actor, workspace)
        now = timezone.now()
        figures = selectors.dashboard(scope, now=now)
        data = {
            "currency": settings.CRM_CURRENCY,
            "time_zone": settings.CRM_TIME_ZONE,
            "business_date": figures.business_date,
            "leads": figures.leads,
            "pipeline": figures.pipeline,
            "activities": figures.activities,
            "new_leads": figures.new_leads,
            "upcoming_meetings": figures.upcoming_meetings,
            "next_tasks": figures.next_tasks,
        }
        # The activity rows render their lead only if it is visible in this scope, and
        # whether they are overdue at the same `now` the figures used.
        context = {
            "scope": scope,
            "now": now,
            "new_lead_opportunities": figures.new_lead_opportunities,
        }
        return Response(s.DashboardSerializer(data, context=context).data)
