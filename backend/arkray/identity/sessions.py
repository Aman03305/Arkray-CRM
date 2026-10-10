"""Server-side session lifecycle (docs/authorization.md#sessions).

- **Sign-in** always issues a brand-new session key (no fixation, even when the same user
  signs in again on an existing session) and a new CSRF token.
- **Absolute lifetime** (SESSION_COOKIE_AGE): enforced by the middleware from the sign-in
  time stored in the session (AUTH_AT), so activity never extends it. The cookie itself is a
  browser-session cookie (identity.session_store): closing the browser ends it, so a shared
  computer doesn't hand the next person an open session (privacy remediation P2-12). The
  row in the database keeps its pinned expiry and is purged after it.
- **Idle timeout** (SESSION_IDLE_TIMEOUT_S): last activity is recorded in the session at
  most every SESSION_ACTIVITY_REFRESH_S.
- **Revocation** is Django's per-request session-hash check: the hash covers the password
  and `User.session_epoch`, and Django's backend rejects inactive users. So deactivation
  and password changes end sessions on their very next request, with no sweep needed.

A session missing its timestamps is treated as expired: deny by default.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import SESSION_KEY, login, logout
from django.contrib.sessions.backends.base import SessionBase
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

from .models import User

AUTH_AT = "_arkray_auth_at"  # epoch seconds of sign-in
SEEN_AT = "_arkray_seen_at"  # epoch seconds of last recorded activity
_CLOCK_SKEW_S = 60
_BACKEND = "django.contrib.auth.backends.ModelBackend"


def _epoch() -> float:
    return time.time()


def start_session(request: HttpRequest, user: User) -> None:
    previous_key = request.session.session_key
    login(request, user, backend=_BACKEND)
    if previous_key is not None and request.session.session_key == previous_key:
        # login() keeps the key when the same user re-authenticates; we never do.
        request.session.cycle_key()
    now = _epoch()
    request.session[AUTH_AT] = now
    request.session[SEEN_AT] = now
    # The row's expiry, pinned (activity never extends it); the cookie itself is a
    # browser-session cookie (identity.session_store).
    request.session.set_expiry(timezone.now() + timedelta(seconds=settings.SESSION_COOKIE_AGE))


def end_session(request: HttpRequest) -> None:
    logout(request)  # deletes the session row and replaces request.user


def session_expired(session: SessionBase, now: float) -> bool:
    auth_at, seen_at = session.get(AUTH_AT), session.get(SEEN_AT)
    if not isinstance(auth_at, int | float) or not isinstance(seen_at, int | float):
        return True
    if auth_at > now + _CLOCK_SKEW_S or seen_at > now + _CLOCK_SKEW_S:
        return True
    return bool(
        now - auth_at >= settings.SESSION_COOKIE_AGE
        or now - seen_at >= settings.SESSION_IDLE_TIMEOUT_S
    )


class SessionPolicyMiddleware:
    """Ends sessions past their idle or absolute limit; records activity. Runs after
    Django's AuthenticationMiddleware."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        session = request.session
        user_id = session.get(SESSION_KEY)
        if user_id is not None:
            now = _epoch()
            if session_expired(session, now):
                end_session(request)
            else:
                seen_at = session[SEEN_AT]
                if now - seen_at >= settings.SESSION_ACTIVITY_REFRESH_S:
                    session[SEEN_AT] = now
        return self.get_response(request)
