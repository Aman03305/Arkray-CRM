"""Building blocks shared by every module's HTTP layer.

- `ApiView`: the base class for API views. CSRF is enforced on every unsafe request
  (authenticated or not), and responses are never cached (they carry personal data).
- `StrictInputSerializer`: input serializers reject undeclared keys, so mass-assignment
  attempts (`"is_superuser": true`, `"capabilities": [...]`) fail loudly instead of being
  silently ignored. Validate query parameters with one too: filters are allowlisted and
  anything else (`?owner=`, `?email__contains=`) is a 400.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from django.utils.cache import add_never_cache_headers
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.permissions import SAFE_METHODS
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .csrf import enforce_csrf
from .errors import InvalidInputError

_MAX_REPORTED_KEYS = 10
IDEMPOTENCY_HEADER = "Idempotency-Key"
_CANONICAL_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


class ApiView(APIView):
    # Methods whose query parameters the view validates itself (with a strict serializer).
    # Any other request carrying query parameters is refused, so no endpoint silently
    # ignores what a client (or a prober) sends: "?owner=..." on a create is a 400.
    query_param_methods: frozenset[str] = frozenset()

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        if request.method not in SAFE_METHODS:
            enforce_csrf(request)
        super().initial(request, *args, **kwargs)
        method = "GET" if request.method == "HEAD" else request.method
        if request.query_params and method not in self.query_param_methods:
            raise serializers.ValidationError(
                {"non_field_errors": [_unknown_keys_message(request.query_params.keys())]}
            )

    def finalize_response(
        self, request: Request, response: Response, *args: Any, **kwargs: Any
    ) -> Response:
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)
        return response


def _printable(key: object) -> str:
    """A key as it may be echoed back: truncated, lone surrogates escaped."""
    return str(key)[:50].encode("utf-8", "backslashreplace").decode("utf-8")


def _unknown_keys_message(keys: Iterable[str]) -> str:
    listed = sorted(_printable(k) for k in keys)
    shown = ", ".join(listed[:_MAX_REPORTED_KEYS])
    more = (
        f" (+{len(listed) - _MAX_REPORTED_KEYS} more)" if len(listed) > _MAX_REPORTED_KEYS else ""
    )
    return f"Unknown field(s): {shown}{more}."


# An input serializer that refuses keys it does not declare as writable fields. (A comment,
# not a docstring: OpenAPI would copy a docstring into every request schema's description.)
class StrictInputSerializer(serializers.Serializer[Any]):
    def to_internal_value(self, data: Any) -> Any:
        if isinstance(data, Mapping):
            writable = {name for name, field in self.fields.items() if not field.read_only}
            unknown = set(data) - writable
            if unknown:
                raise serializers.ValidationError(
                    {"non_field_errors": [_unknown_keys_message(unknown)]}
                )
        return super().to_internal_value(data)


def idempotency_key(request: Request) -> UUID | None:
    """The request's Idempotency-Key (a canonical UUID), or None. Anything else is a 400."""
    raw = request.headers.get(IDEMPOTENCY_HEADER)
    if raw is None:
        return None
    if not _CANONICAL_UUID.fullmatch(raw.strip()):
        message = f"{IDEMPOTENCY_HEADER} must be a UUID."
        raise InvalidInputError(message, details={"idempotency_key": [message]})
    return UUID(raw.strip())


def validated[S: serializers.Serializer[Any]](
    serializer_class: type[S], data: Any
) -> dict[str, Any]:
    """Run an input serializer; invalid input raises (400, standard error envelope)."""
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return dict(serializer.validated_data)


@extend_schema_field(
    {
        "oneOf": [
            {"type": "string", "pattern": r"^[0-9]+(\.[0-9]{1,2})?$", "example": "1250000.00"},
            {"type": "integer", "minimum": 0},
        ]
    }
)
class ExactDecimalField(serializers.Field):  # type: ignore[type-arg]
    """A non-negative decimal amount with at most two decimal places, e.g. money
    ("1250000.50") or a percentage ("62.5"), sent as a JSON string (or an integer).

    JSON numbers with a fraction are refused: the JSON parser turns them into binary
    floats, which can't hold most decimal amounts exactly. So are exponents ("1e6"), signs,
    separators, NaN and Infinity: the only accepted spelling is plain digits with an
    optional decimal point, so no parser quirk can reinterpret an amount."""

    default_error_messages = {
        "invalid": "Enter a number such as 1250000 or 1250000.50 (digits and a decimal point).",
        "type": 'Send the number as a string, e.g. "1250000.50".',
        "max_whole_digits": "Use at most {max_whole_digits} digits before the decimal point.",
    }
    # ASCII digits only: \d would also accept other scripts' digits, which Decimal() reads.
    _PATTERN = re.compile(r"([0-9]+)(?:\.([0-9]{1,2}))?")

    def __init__(self, *, max_whole_digits: int, **kwargs: Any) -> None:
        self.max_whole_digits = max_whole_digits
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> Decimal:
        if isinstance(data, bool) or not isinstance(data, str | int):
            self.fail("type")
        text = str(data)
        match = self._PATTERN.fullmatch(text)
        if match is None:
            self.fail("invalid")
        if len(match.group(1).lstrip("0")) > self.max_whole_digits:
            self.fail("max_whole_digits", max_whole_digits=self.max_whole_digits)
        return Decimal(text)

    def to_representation(self, value: Decimal) -> str:
        return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


class AwareDateTimeField(serializers.DateTimeField):
    """An ISO 8601 timestamp that must state its offset. DRF would silently read a naive
    value as UTC; a wrong guess by hours is worse than a clear error."""

    default_error_messages = {
        "naive": "Include a time zone offset, for example 2026-09-30T10:15:00+05:30.",
    }

    def to_internal_value(self, value: Any) -> Any:
        if isinstance(value, str):
            try:
                parsed = parse_datetime(value.strip())
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is None:
                self.fail("naive")
        return super().to_internal_value(value)
