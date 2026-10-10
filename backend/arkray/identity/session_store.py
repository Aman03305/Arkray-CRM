"""The session store: Django's database sessions, with browser-session cookies
(identity.sessions; privacy remediation P2-12).

A session's row keeps the expiry pinned at sign-in (SESSION_COOKIE_AGE; activity never
extends it), but the cookie carries no expiry of its own: closing the browser ends it, so a
shared computer doesn't hand the next person a signed-in session. The idle and absolute
limits are enforced server-side whatever the browser does (identity.sessions).
"""

from __future__ import annotations

from django.contrib.sessions.backends.db import SessionStore as DatabaseSessionStore


class SessionStore(DatabaseSessionStore):
    def get_expire_at_browser_close(self) -> bool:
        return True
