"""Durable authentication throttling, backed by PostgreSQL (never only Redis).

Policy (docs/authorization.md#sign-in-throttling, ADR-0014), all windows sliding:

- **Per account and browser.** Failures are counted per *submitted* email (a keyed HMAC,
  so unknown emails are throttled exactly like real ones and nothing reveals whether an
  account exists) and per browser: an unrecognised browser shares one budget, while a
  browser holding a valid trusted-device cookie (set after a successful sign-in to that
  account) has its own. After LOGIN_ACCOUNT_FAILURE_THRESHOLD failures in the window the
  bucket is locked for LOGIN_LOCKOUT_BASE_S, doubling with each further failure, capped at
  LOGIN_LOCKOUT_MAX_S. So guessing is slow, lockouts are always temporary, and an attacker
  cannot lock the real user out of the browser they normally use.
- **Per source** (client IP, trusted-proxy aware; an IPv6 address counts as its /64, the
  usual size of one subscriber's allocation; an unknown address shares one bucket, so a
  proxy quirk fails closed): LOGIN_SOURCE_FAILURE_THRESHOLD failures in the window block
  further attempts from unrecognised browsers at that source. This catches password
  spraying from one source. A trusted browser is exempt, so a noisy neighbour behind the
  same office NAT cannot lock the owner out either.
- **Atomic reservations.** An attempt first *reserves* a failure (a short transaction that
  holds advisory locks on the account and the source, checks both budgets and inserts the
  failure row) and only then checks the password. A correct password deletes the
  reservation. So a parallel burst can never exceed a budget, the password hasher never
  runs inside a transaction, and nobody is refused just because another attempt is in
  flight. Locks are held for a count and an insert only.
- Refused attempts are refused *before* the password is checked (no oracle) and are not
  recorded, so an attack cannot grow the table without bound.
- Keys survive `SECRET_KEY` rotation: identifiers and trusted-device cookies made with a key
  in SECRET_KEY_FALLBACKS still count and still match.

Queries read at most a few dozen index entries (partial indexes on the failure log), so
they stay cheap under attack. Crossing a threshold is audited once, without the email.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from django.conf import settings
from django.core import signing
from django.db import connection, transaction
from django.http import HttpRequest, HttpResponseBase
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from arkray.audit import services as audit

from .models import AuthThrottleEvent, ThrottleKind, normalize_email

AUDIT_ACTION_LOGIN_THROTTLED = "auth.login_throttled"
_IDENTIFIER_SALT = "arkray.identity.login-identifier"
_DEVICE_SALT = "arkray.identity.login-device"
_DEVICE_ID = re.compile(r"^[0-9a-f]{32}$")
# Advisory-lock namespaces ("ARK1", "ARK3"; "ARK2" is the user-administration lock).
_ACCOUNT_LOCK = 0x41524B31
_SOURCE_LOCK = 0x41524B33
# The API only: sign-in and the re-authentication of account changes (password, own
# email: /api/v1/admin/users/<id>/change-email), never the pages.
DEVICE_COOKIE_PATH = "/api/v1/"
_DEVICE_TRUST_SALT = "arkray.identity.login-device-trust"
RESET_REQUEST_WINDOW = timedelta(hours=1)
IPV6_SOURCE_PREFIX = 64


@dataclass(frozen=True, slots=True)
class Denied:
    retry_after: int  # seconds
    reason: Literal["account", "source"]


# --- keys --------------------------------------------------------------------------------
def login_identifiers(email: str) -> list[str]:
    """Keyed hashes of the normalised email under the current key, then any fallback keys
    (so rotating SECRET_KEY neither lifts lockouts nor untrusts browsers)."""
    normalised = normalize_email(email)
    return [
        salted_hmac(_IDENTIFIER_SALT, normalised, secret=key, algorithm="sha256").hexdigest()
        for key in [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
    ]


def login_identifier(email: str) -> str:
    """The current keyed hash of the normalised email: the throttling key, never reversible."""
    return login_identifiers(email)[0]


def source_key(ip: str | None) -> str | None:
    """The throttling key for a client address: IPv4 as is, IPv6 reduced to its /64."""
    if ip is None:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        network = ipaddress.IPv6Network((address, IPV6_SOURCE_PREFIX), strict=False)
        return str(network.network_address)
    return str(address)


def _lock(namespace: int, key: str) -> None:
    """Transaction-scoped advisory lock, held for a count and an insert (milliseconds)."""
    digest = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [namespace, digest])


def _source_filter(source: str | None) -> dict[str, object]:
    # An unknown source is one shared bucket (fail closed), not "no limit".
    return {"ip_address__isnull": True} if source is None else {"ip_address": source}


def lockout_seconds(failures: int) -> int:
    threshold = settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD
    if failures < threshold:
        return 0
    lockout = settings.LOGIN_LOCKOUT_BASE_S * 2 ** (failures - threshold)
    return int(min(lockout, settings.LOGIN_LOCKOUT_MAX_S))


def _backoff_steps() -> int:
    """How many failures past the threshold still lengthen the lockout."""
    ratio = settings.LOGIN_LOCKOUT_MAX_S / settings.LOGIN_LOCKOUT_BASE_S
    return max(1, math.ceil(math.log2(ratio)) + 1)


def _seconds_until(moment: datetime, now: datetime) -> int:
    return max(1, math.ceil((moment - now).total_seconds()))


# --- trusted-device cookie ---------------------------------------------------------------
def device_trust(password_hash: str, session_epoch: int, *, secret: str | None = None) -> str:
    """What a trusted-device cookie was issued against: the account's credentials as they
    were (a keyed hash; neither value is recoverable). A password reset or change, or
    anything that ends every session (`session_epoch`), retires every trusted browser:
    each must sign in successfully again (Phase 9 review: cookies harvested by someone who
    once knew the password kept their own budgets against the new one)."""
    return salted_hmac(
        _DEVICE_TRUST_SALT, f"{password_hash}|{session_epoch}", secret=secret, algorithm="sha256"
    ).hexdigest()[:32]


def _current_trusts(email: str) -> list[str]:
    """The account's trust under the current key, then any fallback keys (rotating
    SECRET_KEY keeps browsers trusted, like the login identifiers)."""
    from .models import User

    row = (
        User.objects.filter(email=normalize_email(email))
        .values_list("password", "session_epoch")
        .first()
    )
    if row is None:
        return []
    return [
        device_trust(*row, secret=key)
        for key in [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
    ]


def read_device(request: HttpRequest, identifiers: Sequence[str], email: str) -> str:
    """The trusted-device id for this account, or "" for an unrecognised browser (or one
    trusted before the account's credentials last changed). The account is read only for a
    validly signed cookie bound to this email, which an attacker can't forge."""
    raw = request.COOKIES.get(settings.LOGIN_DEVICE_COOKIE_NAME)
    if not raw:
        return ""
    try:
        data = signing.loads(raw, salt=_DEVICE_SALT, max_age=settings.LOGIN_DEVICE_COOKIE_AGE_S)
    except signing.BadSignature:
        return ""
    if not isinstance(data, dict):
        return ""
    bound_to, device = data.get("i"), data.get("d")
    if not isinstance(bound_to, str):
        return ""
    if not any(constant_time_compare(bound_to, identifier) for identifier in identifiers):
        return ""
    if not isinstance(device, str) or not _DEVICE_ID.fullmatch(device):
        return ""
    trust = data.get("t")
    if not isinstance(trust, str):
        return ""
    if not any(constant_time_compare(trust, current) for current in _current_trusts(email)):
        return ""
    return device


def remember_device(
    response: HttpResponseBase, identifier: str, device_id: str, trust: str
) -> None:
    response.set_cookie(
        settings.LOGIN_DEVICE_COOKIE_NAME,
        signing.dumps({"i": identifier, "d": device_id, "t": trust}, salt=_DEVICE_SALT),
        max_age=settings.LOGIN_DEVICE_COOKIE_AGE_S,
        path=DEVICE_COOKIE_PATH,
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="Strict",
    )


# --- sign-in throttle --------------------------------------------------------------------
class LoginThrottle:
    """Throttle state for one attempt to prove a password:

    throttle = LoginThrottle(login_identifiers(email), device_id, ip)
    denied = throttle.reserve()          # short transaction; None if allowed
    ... verify the password (no transaction held) ...
    throttle.succeeded() | throttle.failed()
    """

    def __init__(self, identifiers: Sequence[str], device_id: str, ip: str | None) -> None:
        self.identifiers = list(identifiers)
        self.identifier = self.identifiers[0]
        self.device_id = device_id
        self.source = source_key(ip)
        self.now = timezone.now()
        self._window_start = self.now - timedelta(seconds=settings.LOGIN_THROTTLE_WINDOW_S)
        self._account_failures = 0
        self._source_failures = 0
        self._reservation: int | None = None

    def check(self) -> Denied | None:
        """Read-only budget check (reserve() runs it under the locks)."""
        recent = list(
            AuthThrottleEvent.objects.filter(
                kind=ThrottleKind.LOGIN_FAILURE,
                identifier_hash__in=self.identifiers,
                device_id=self.device_id,
                occurred_at__gte=self._window_start,
            )
            .order_by("-occurred_at")
            .values_list("occurred_at", flat=True)[
                : settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD + _backoff_steps()
            ]
        )
        self._account_failures = len(recent)
        lockout = lockout_seconds(len(recent))
        if lockout:
            locked_until = recent[0] + timedelta(seconds=lockout)
            if self.now < locked_until:
                return Denied(_seconds_until(locked_until, self.now), "account")

        limit = settings.LOGIN_SOURCE_FAILURE_THRESHOLD
        from_source = list(
            AuthThrottleEvent.objects.filter(
                kind=ThrottleKind.LOGIN_FAILURE,
                occurred_at__gte=self._window_start,
                **_source_filter(self.source),
            )
            .order_by("-occurred_at")
            .values_list("occurred_at", flat=True)[:limit]
        )
        self._source_failures = len(from_source)
        if len(from_source) >= limit and not self.device_id:
            # Unblocked once the oldest of the last `limit` failures leaves the window.
            unblocked_at = from_source[-1] + timedelta(seconds=settings.LOGIN_THROTTLE_WINDOW_S)
            return Denied(_seconds_until(unblocked_at, self.now), "source")
        return None

    def reserve(self) -> Denied | None:
        """Atomically check both budgets and record this attempt as a failure."""
        with transaction.atomic():
            _lock(_ACCOUNT_LOCK, self.identifier)  # account first, then source: no cycles
            _lock(_SOURCE_LOCK, self.source or "unknown")
            denied = self.check()
            if denied is None:
                self._reservation = AuthThrottleEvent.objects.create(
                    kind=ThrottleKind.LOGIN_FAILURE,
                    occurred_at=self.now,
                    identifier_hash=self.identifier,
                    device_id=self.device_id,
                    ip_address=self.source,
                ).pk
        return denied

    def failed(self) -> None:
        """The reservation stands as the failure. Audit each threshold crossing once (not
        every failure: that would be noise, and nothing here identifies the account)."""
        if self._account_failures + 1 == settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD:
            audit.record(
                AUDIT_ACTION_LOGIN_THROTTLED,
                actor_id=None,
                target_type="login_identifier",
                target_id=self.identifier[:32],
                metadata={"scope": "account", "trusted_device": bool(self.device_id)},
            )
        if self._source_failures + 1 == settings.LOGIN_SOURCE_FAILURE_THRESHOLD:
            audit.record(
                AUDIT_ACTION_LOGIN_THROTTLED,
                actor_id=None,
                target_type="login_source",
                metadata={"scope": "source"},
            )

    def succeeded(self) -> None:
        """A proven password resets this browser's budget for the account (reservation
        included)."""
        AuthThrottleEvent.objects.filter(
            kind=ThrottleKind.LOGIN_FAILURE,
            identifier_hash__in=self.identifiers,
            device_id=self.device_id,
        ).delete()


def forget_account_failures(email: str) -> None:
    """After the owner proves control of the mailbox (reset, activation), start afresh."""
    AuthThrottleEvent.objects.filter(
        kind=ThrottleKind.LOGIN_FAILURE, identifier_hash__in=login_identifiers(email)
    ).delete()


# --- password-reset requests --------------------------------------------------------------
def reserve_reset_request(ip: str | None) -> Denied | None:
    """Atomically check the per-source limit and record the request. Call in a transaction."""
    source = source_key(ip)
    _lock(_SOURCE_LOCK, f"reset:{source or 'unknown'}")
    now = timezone.now()
    limit = settings.PASSWORD_RESET_SOURCE_LIMIT_PER_HOUR
    recent = list(
        AuthThrottleEvent.objects.filter(
            kind=ThrottleKind.PASSWORD_RESET_REQUEST,
            occurred_at__gte=now - RESET_REQUEST_WINDOW,
            **_source_filter(source),
        )
        .order_by("-occurred_at")
        .values_list("occurred_at", flat=True)[:limit]
    )
    if len(recent) >= limit:
        return Denied(_seconds_until(recent[-1] + RESET_REQUEST_WINDOW, now), "source")
    AuthThrottleEvent.objects.create(
        kind=ThrottleKind.PASSWORD_RESET_REQUEST, occurred_at=now, ip_address=source
    )
    return None


def purge_expired(now: datetime | None = None) -> int:
    cutoff = (now or timezone.now()) - timedelta(seconds=settings.AUTH_THROTTLE_RETENTION_S)
    deleted, _ = AuthThrottleEvent.objects.filter(occurred_at__lt=cutoff).delete()
    return deleted
