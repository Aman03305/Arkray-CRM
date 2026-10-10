"""Privacy administration: data-subject exports and staff pseudonymisation (privacy.manage,
administrators only, never inside a support session). Mounted under /api/v1/admin/privacy/;
every route is in tests/authz_matrix.py."""

from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from django.db.models import QuerySet
from django.http import StreamingHttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status as http
from rest_framework.request import Request
from rest_framework.response import Response

from arkray.activities import storage
from arkray.core.api import ApiView, validated
from arkray.core.errors import BusinessRuleViolation, NotFoundError
from arkray.identity.models import User, normalize_email
from arkray.identity.permissions import requires
from arkray.identity.policy import Capability

from .. import exports, staff
from ..models import DataExport
from . import serializers as s

LIST_LIMIT = 50


def _actor(request: Request) -> User:
    user = request.user
    assert isinstance(user, User)  # noqa: S101 — the permission class guarantees it
    return user


def _mine(actor: User) -> QuerySet[DataExport]:
    return DataExport.objects.filter(requested_by=actor).order_by("-created_at")


class DataExportListView(ApiView):
    """Exports you requested (the newest 50), and requesting one (202: a job builds it)."""

    permission_classes = [requires(Capability.PRIVACY_MANAGE)]
    refused_in_support_session = True

    @extend_schema(operation_id="privacy_exports_list", responses={200: s.DataExportListSerializer})
    def get(self, request: Request) -> Response:
        return Response(
            {"results": s.DataExportSerializer(_mine(_actor(request))[:LIST_LIMIT], many=True).data}
        )

    @extend_schema(
        operation_id="privacy_exports_request",
        request=s.ExportRequestSerializer,
        responses={202: s.DataExportSerializer},
    )
    def post(self, request: Request) -> Response:
        data = validated(s.ExportRequestSerializer, request.data)
        export = exports.request(
            actor=_actor(request),
            subject_type=data["subject_type"],
            subject_id=data["subject_id"],
            reference=data["reference"],
        )
        return Response(s.DataExportSerializer(export).data, status=http.HTTP_202_ACCEPTED)


class DataExportDetailView(ApiView):
    permission_classes = [requires(Capability.PRIVACY_MANAGE)]
    refused_in_support_session = True

    @extend_schema(operation_id="privacy_exports_retrieve", responses={200: s.DataExportSerializer})
    def get(self, request: Request, export_id: UUID) -> Response:
        export = _mine(_actor(request)).filter(pk=export_id).first()
        if export is None:
            raise NotFoundError()
        return Response(s.DataExportSerializer(export).data)


class DataExportDownloadView(ApiView):
    """The export's ZIP, for the administrator who requested it, until it expires."""

    permission_classes = [requires(Capability.PRIVACY_MANAGE)]
    refused_in_support_session = True

    @extend_schema(
        operation_id="privacy_exports_download",
        responses={(200, "application/zip"): OpenApiTypes.BINARY},
    )
    def get(self, request: Request, export_id: UUID) -> StreamingHttpResponse:
        export, file = exports.for_download(actor=_actor(request), export_id=export_id)
        response = StreamingHttpResponse(storage.iter_file(file), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="arkray-export-{export.pk}.zip"'
        if export.size is not None:
            response["Content-Length"] = str(export.size)
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        return response


class PseudonymiseUserView(ApiView):
    """Pseudonymise a deactivated user (privacy.staff), confirming them by their current
    email. Irreversible."""

    permission_classes = [requires(Capability.PRIVACY_MANAGE)]
    refused_in_support_session = True

    @extend_schema(
        operation_id="privacy_pseudonymise_user",
        request=s.PseudonymiseSerializer,
        responses={200: s.PseudonymiseResultSerializer},
    )
    def post(self, request: Request, user_id: UUID) -> Response:
        actor = _actor(request)
        data = validated(s.PseudonymiseSerializer, request.data)
        user = User.objects.filter(pk=user_id).first()
        if user is None:
            raise NotFoundError()
        if normalize_email(data["confirm_email"]) != user.email:
            raise BusinessRuleViolation("Type the account's email address to confirm.")
        try:
            result = staff.pseudonymise(user_id, operator_id=actor.pk)
        except staff.AlreadyPseudonymised as exc:
            raise BusinessRuleViolation(str(exc)) from None
        body = {k: v for k, v in asdict(result).items() if k != "user_id"}
        return Response(s.PseudonymiseResultSerializer(body).data)
