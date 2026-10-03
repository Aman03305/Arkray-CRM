"""Shared HTTP building blocks: ApiView, strict input, CSRF, 401 challenge, rate limits."""

import pytest
from django.test import RequestFactory
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from arkray.core.api import ApiView, StrictInputSerializer
from arkray.core.authentication import SessionAuthentication
from arkray.core.csrf import CsrfFailed
from arkray.core.errors import RateLimitedError
from arkray.core.exceptions import api_exception_handler


class NameSerializer(StrictInputSerializer):
    name = serializers.CharField()
    id = serializers.CharField(read_only=True)


class TestStrictInput:
    def test_accepts_declared_fields(self):
        serializer = NameSerializer(data={"name": "Rahul"})
        assert serializer.is_valid(), serializer.errors

    @pytest.mark.parametrize(
        "extra", [{"is_superuser": True}, {"id": "x"}, {"capabilities": ["*"]}]
    )
    def test_rejects_undeclared_and_read_only_keys(self, extra):
        serializer = NameSerializer(data={"name": "Rahul", **extra})
        assert not serializer.is_valid()
        assert "Unknown field(s)" in serializer.errors["non_field_errors"][0]

    def test_the_error_message_is_bounded(self):
        serializer = NameSerializer(data={"name": "x", **{f"k{i:02}": 1 for i in range(40)}})
        serializer.is_valid()
        message = serializer.errors["non_field_errors"][0]
        assert message.endswith("(+30 more).")
        assert len(message) < 200


def test_query_parameters_are_allowlisted_the_same_way():
    request = Request(APIRequestFactory().get("/", {"name": "x", "owner": "b"}))
    serializer = NameSerializer(data=request.query_params)
    assert not serializer.is_valid()
    assert "owner" in serializer.errors["non_field_errors"][0]


class EchoView(ApiView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"ok": True})

    def post(self, request):
        return Response({"ok": True})


@pytest.mark.django_db
class TestApiView:
    def test_responses_are_never_cached(self):
        response = EchoView.as_view()(APIRequestFactory().get("/"))
        assert "no-store" in response["Cache-Control"]

    def test_csrf_is_enforced_for_anonymous_unsafe_requests(self):
        request = APIRequestFactory(enforce_csrf_checks=True).post("/", {}, format="json")
        response = EchoView.as_view()(request)
        assert response.status_code == 403
        assert response.data["error"]["code"] == "csrf_failed"

    def test_safe_requests_need_no_token(self):
        request = APIRequestFactory(enforce_csrf_checks=True).get("/")
        assert EchoView.as_view()(request).status_code == 200


def test_unauthenticated_requests_are_challenged_so_they_get_401():
    request = Request(RequestFactory().get("/"))
    assert SessionAuthentication().authenticate_header(request) == 'Session realm="arkray"'


class TestErrorMapping:
    def test_rate_limits_carry_retry_after(self):
        response = api_exception_handler(RateLimitedError(retry_after=42), {})
        assert (response.status_code, response["Retry-After"]) == (429, "42")
        assert response.data["error"]["code"] == "rate_limited"

    def test_retry_after_is_at_least_one_second(self):
        assert RateLimitedError(retry_after=0).retry_after == 1

    def test_csrf_failures_have_their_own_code(self):
        response = api_exception_handler(CsrfFailed(), {})
        assert (response.status_code, response.data["error"]["code"]) == (403, "csrf_failed")
