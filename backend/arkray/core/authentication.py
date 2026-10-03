"""Session authentication for the API.

Identical to DRF's SessionAuthentication except that:
- an unauthenticated request gets **401** (DRF answers 403 when the authenticator offers no
  `WWW-Authenticate` challenge), so clients can tell "sign in" apart from "not allowed";
- CSRF failures use the standard `csrf_failed` error code;
- the verified user's id (never a raw session value) is added to the log context.
"""

from __future__ import annotations

from typing import Any

from rest_framework import authentication
from rest_framework.request import Request

from .context import update_context
from .csrf import enforce_csrf


class SessionAuthentication(authentication.SessionAuthentication):
    def authenticate(self, request: Request) -> tuple[Any, None] | None:
        result = super().authenticate(request)
        if result is not None:
            # Only a session that passed every check (hash, active user) labels log lines.
            update_context(user_id=str(result[0].pk))
        return result

    def authenticate_header(self, request: Request) -> str:
        return 'Session realm="arkray"'

    def enforce_csrf(self, request: Request) -> None:
        enforce_csrf(request)
