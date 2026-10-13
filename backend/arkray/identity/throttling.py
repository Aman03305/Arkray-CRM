"""Durable authentication throttling, backed by PostgreSQL (never only Redis).

Policy (docs/authorization.md#sign-in-throttling, ADR-0014), all windows sliding:

- **Per account and browser.** Failures are counted per *submitted* email (a keyed HMAC,
  so unknown emails are throttled exactly like real ones and nothing reveals whether an
  account exists) and per browser: an unrecognised browser's budget is per account and
  *network* (an IPv4 /24, an IPv6 /64; adversarial review of 73ff1e3, L1: one shared
  budget let anyone who knew an email keep its owner locked out of every new browser),
  while a browser holding a valid trusted-device cookie (set after a successful sign-in to
  that account) has its own. After LOGIN_ACCOUNT_FAILURE_THRESHOLD failures in the window
  the bucket is locked for LOGIN_LOCKOUT_BASE_S, doubling with each further failure, capped
  at LOGIN_LOCKOUT_MAX_S. So guessing is slow, lockouts are always temporary, a storm from
  one network doesn't refuse the right password from another, and an attacker cannot lock
  the real user out of the browser they normally use.
- **Per account and day** (L1): LOGIN_DAILY_FAILURE_CAP failures from unrecognised
  browsers within LOGIN_DAILY_WINDOW_S, whatever networks they came from, lock the account's
  password sign-in from every unrecognised browser (`LoginLock`), so guessing stops instead
  of going on at the lockouts' pace (about 550 a day before) for ever. Browsers that signed
  in to the account before keep working. A success from an unrecognised browser forgives
  that browser's lockout but not the day (`forgiven`). The owner is emailed an unlock link
  (identity.services); a password reset, an accepted invitation, a password set by an
  administrator or an administrator's "clear" lift it too, and it lifts by itself after
  LOGIN_LOCK_MAX_AGE_S. Keyed by the submitted email like the rest: an unknown email is
  locked alike.
- **Per source** (client IP, trusted-proxy aware; an IPv6 address counts as its /64, the
  usual size of one subscriber's allocation; an unknown address shares one bucket, so a
  proxy quirk fails closed): LOGIN_SOURCE_FAILURE_THRESHOLD failures from unrecognised
  browsers in the window block further attempts from unrecognised browsers at that source.
  This catches password spraying from one source. A trusted browser is exempt, and its own
  failures (its owner's typos) don't count, so a noisy neighbour behind the same office NAT
  cannot lock the owner out either.
- **Atomic reservations.** An attempt first *reserves* a failure (a short transaction that
  holds advisory locks on the account and the source, checks the budgets and inserts the
  failure row) and only then checks the password. A correct password withdraws the
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
import logging
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from django.conf import settings
from django.core import signing
from django.core.checks import Error, register
from django.db import connection, transaction
from django.db.models import BooleanField, F, Func, GenericIPAddressField, Value
from django.db.models.functions import Cast
from django.http import HttpRequest, HttpResponseBase
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from arkray.audit import services as audit
from arkray.core import outbox
from arkray.core.errors import RateLimitedError
from arkray.core.middleware import client_ip

from .models import AuthThrottleEvent, LoginLock, ThrottleKind, normalize_email

logger = logging.getLogger(__name__)

AUDIT_ACTION_LOGIN_THROTTLED = "auth.login_throttled"
# The daily cap was reached: email the account's owner, if there is one (identity.handlers).
TOPIC_SIGN_IN_LOCKED = "identity.sign_in_locked"
_IDENTIFIER_SALT = "arkray.identity.login-identifier"
_DEVICE_SALT = "arkray.identity.login-device"
_DEVICE_ID = re.compile(r"^[0-9a-f]{32}$")
# Advisory-lock namespaces ("ARK1", "ARK5"; "ARK2" is the user-administration lock). The
# source lock was "ARK3", which pipeline.configuration's per-owner lock also used (review of
# 73ff1e3: tests/architecture/test_advisory_locks.py keeps every namespace to one owner).
_ACCOUNT_LOCK = 0x41524B31
_SOURCE_LOCK = 0x41524B35
# The API only: sign-in and the re-authentication of account changes (password, own
# email: /api/v1/admin/users/<id>/change-email), never the pages.
DEVICE_COOKIE_PATH = "/api/v1/"
_DEVICE_TRUST_SALT = "arkray.identity.login-device-trust"
RESET_REQUEST_WINDOW = timedelta(hours=1)
IPV6_SOURCE_PREFIX = 64
SIGN_IN_LOCKED = (
    "Too many failed sign-ins for this account, so it can't be signed in to from a new"
    " browser for now. Sign in from a browser you've used here before, use the link we"
    " emailed to the account's address, or reset your password."
)


@dataclass(frozen=True, slots=True)
class Denied:
    retry_after: int  # seconds
    reason: Literal["account", "source", "locked"]


class SignInLockedError(RateLimitedError):
    """The account's daily cap locked password sign-in from unrecognised browsers (L1).
    Identical for an unknown email: nothing tells the two apart."""

    code = "sign_in_locked"
    default_message = SIGN_IN_LOCKED


# --- configuration ----------------------------------------------------------------------------
@register()
def check_daily_window(**_: Any) -> list[Error]:
    """The daily cap counts the failure evidence, which housekeeping purges after
    AUTH_THROTTLE_RETENTION_S: a longer window would silently count less than it says."""
    window, retention = settings.LOGIN_DAILY_WINDOW_S, settings.AUTH_THROTTLE_RETENTION_S
    if 0 < window <= retention and settings.LOGIN_DAILY_FAILURE_CAP > 0:
        return []
    return [
        Error(
            "LOGIN_DAILY_WINDOW_S must be positive and at most AUTH_THROTTLE_RETENTION_S, and"
            " LOGIN_DAILY_FAILURE_CAP positive (review of 73ff1e3, L1).",
            id="arkray.E031",
        )
    ]


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


def network_key(ip: str | None) -> str | None:
    """The network an unrecognised browser's budget is kept per (L1): an IPv4 address's /24
    (LOGIN_NETWORK_IPV4_PREFIX), an IPv6 address's /64, as CIDR text; None for an unknown
    address (those share one bucket: fail closed)."""
    source = source_key(ip)
    if source is None:
        return None
    address = ipaddress.ip_address(source)
    prefix = settings.LOGIN_NETWORK_IPV4_PREFIX if address.version == 4 else IPV6_SOURCE_PREFIX
    return str(ipaddress.ip_network((address, prefix), strict=False))


class _WithinNetwork(Func):
    """`ip_address <<= '<cidr>'::inet`: PostgreSQL's containment test, as a filter."""

    arg_joiner = " <<= "
    template = "(%(expressions)s)"
    output_field = BooleanField()


def _network_filter(network: str | None) -> dict[str, object] | _WithinNetwork:
    if network is None:
        return {"ip_address__isnull": True}
    return _WithinNetwork(F("ip_address"), Cast(Value(network), GenericIPAddressField()))


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


def held_for_another_account(request: HttpRequest, identifiers: Sequence[str]) -> bool:
    """Whether this browser's trusted-device cookie is a valid one (our signature, unexpired)
    bound to an account other than these identifiers'. There is one cookie per browser: the
    emailed unlock link must never replace a colleague's trust with its own account's (review
    round 1, IDENTITY-1: an insider's link pressed in a colleague's everyday browser took its
    trust away, and the insider could then lock that browser out)."""
    raw = request.COOKIES.get(settings.LOGIN_DEVICE_COOKIE_NAME)
    if not raw:
        return False
    try:
        data = signing.loads(raw, salt=_DEVICE_SALT, max_age=settings.LOGIN_DEVICE_COOKIE_AGE_S)
    except signing.BadSignature:
        return False
    bound_to = data.get("i") if isinstance(data, dict) else None
    if not isinstance(bound_to, str):
        return False
    return not any(constant_time_compare(bound_to, identifier) for identifier in identifiers)


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

    throttle = LoginThrottle(login_identifiers(email), device_id, ip, email)
    denied = throttle.reserve()          # short transaction; None if allowed
    ... verify the password (no transaction held) ...
    throttle.succeeded() | throttle.failed()
    """

    def __init__(
        self, identifiers: Sequence[str], device_id: str, ip: str | None, email: str = ""
    ) -> None:
        self.identifiers = list(identifiers)
        self.identifier = self.identifiers[0]
        self.device_id = device_id
        self.source = source_key(ip)
        self.network = network_key(ip)
        # Normalised; only for the owner's notice when this attempt reaches the daily cap.
        self.email = normalize_email(email)
        self.now = timezone.now()
        self._window_start = self.now - timedelta(seconds=settings.LOGIN_THROTTLE_WINDOW_S)
        self._account_failures = 0
        self._source_failures = 0
        self._reservation: int | None = None

    def _bucket(self) -> Any:
        """This browser's failures for the account: a trusted browser's own, or those of
        unrecognised browsers in this network. Forgiven ones (a success followed) don't count."""
        failures = AuthThrottleEvent.objects.filter(
            kind=ThrottleKind.LOGIN_FAILURE,
            identifier_hash__in=self.identifiers,
            device_id=self.device_id,
            forgiven=False,
        )
        if self.device_id:
            return failures
        network = _network_filter(self.network)
        return failures.filter(network) if isinstance(network, Func) else failures.filter(**network)

    def check(self) -> Denied | None:
        """Read-only budget check (reserve() runs it under the locks)."""
        if not self.device_id:
            locked_at = (
                LoginLock.objects.filter(identifier_hash__in=self.identifiers)
                .order_by("locked_at")
                .values_list("locked_at", flat=True)
                .first()
            )
            if locked_at is not None:
                lifts_at = locked_at + timedelta(seconds=settings.LOGIN_LOCK_MAX_AGE_S)
                return Denied(_seconds_until(lifts_at, self.now), "locked")

        recent = list(
            self._bucket()
            .filter(occurred_at__gte=self._window_start)
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
                # Unrecognised browsers' only: a trusted browser's failures are its owner's
                # typos, which mustn't block the colleagues behind the same address (L13).
                device_id="",
                forgiven=False,
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
        """Atomically check the budgets and record this attempt as a failure."""
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
        every failure: that would be noise, and nothing here identifies the account); from
        an unrecognised browser, lock the account if this failure reached the daily cap."""
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
        if not self.device_id:
            self._lock_if_capped()

    def _lock_if_capped(self) -> None:
        """LOGIN_DAILY_FAILURE_CAP failures from unrecognised browsers in the day (forgiven
        ones included) lock the account's sign-in from them. The same statements whether or
        not the email has an account; the notice job decides whom to tell (as a reset
        request does). Under the account's advisory lock: one lock, one notice."""
        cap = settings.LOGIN_DAILY_FAILURE_CAP
        since = self.now - timedelta(seconds=settings.LOGIN_DAILY_WINDOW_S)
        with transaction.atomic():
            _lock(_ACCOUNT_LOCK, self.identifier)
            failures = AuthThrottleEvent.objects.filter(
                kind=ThrottleKind.LOGIN_FAILURE,
                identifier_hash__in=self.identifiers,
                device_id="",
                occurred_at__gte=since,
            )[:cap].count()
            if failures < cap:
                return
            if LoginLock.objects.filter(identifier_hash__in=self.identifiers).exists():
                return
            LoginLock.objects.create(identifier_hash=self.identifier, locked_at=self.now)
            # Personal data: blanked by identity.housekeeping once handled.
            outbox.enqueue(TOPIC_SIGN_IN_LOCKED, {"email": self.email})
        logger.warning("sign_in_locked", extra={"login": self.identifier[:12]})

    def release(self) -> None:
        """This attempt wasn't a failure (a right password, the second step still to come),
        but nothing is forgiven yet: only this attempt's reservation is withdrawn, earlier
        failures stand until the second factor is proved (ADR-0033)."""
        if self._reservation is not None:
            AuthThrottleEvent.objects.filter(pk=self._reservation).delete()
            self._reservation = None

    def succeeded(self) -> None:
        """A proven password (and second factor) withdraws this attempt's reservation and
        forgives this browser's earlier failures for the account: they no longer lock it.
        They still count toward the daily cap (L1: the owner signing in from a new browser
        forgave an attacker's whole day before)."""
        self.release()
        self._bucket().update(forgiven=True)


def for_request(request: HttpRequest, email: str) -> LoginThrottle:
    """The throttle for proving a credential of the account `email` names, from this browser
    and client address: sign-in, re-authentication and second-factor codes share it."""
    identifiers = login_identifiers(email)
    device_id = read_device(request, identifiers, email)
    return LoginThrottle(identifiers, device_id, client_ip(request), email)


def rate_limited(denied: Denied) -> RateLimitedError:
    if denied.reason == "locked":
        return SignInLockedError(retry_after=denied.retry_after)
    minutes = max(1, round(denied.retry_after / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return RateLimitedError(
        f"Too many sign-in attempts. Try again in {minutes} {unit}.", retry_after=denied.retry_after
    )


def forget_account_failures(email: str) -> int:
    """After the owner proves control of the mailbox (reset, activation, the unlock link) or
    an administrator steps in, start afresh: the failures and the daily lock go. How many
    locks were lifted (0 or 1)."""
    identifiers = login_identifiers(email)
    AuthThrottleEvent.objects.filter(
        kind=ThrottleKind.LOGIN_FAILURE, identifier_hash__in=identifiers
    ).delete()
    lifted, _ = LoginLock.objects.filter(identifier_hash__in=identifiers).delete()
    return int(lifted)


def sign_in_locked_at(email: str) -> datetime | None:
    """When the account's daily cap locked it, or None (for administrators' user views)."""
    return (
        LoginLock.objects.filter(identifier_hash__in=login_identifiers(email))
        .order_by("locked_at")
        .values_list("locked_at", flat=True)
        .first()
    )


def sign_in_locks(emails: Iterable[str]) -> dict[str, datetime]:
    """`sign_in_locked_at` for several accounts in one query: {normalised email: locked_at}."""
    by_identifier = {
        identifier: normalize_email(email)
        for email in emails
        for identifier in login_identifiers(email)
    }
    found: dict[str, datetime] = {}
    for identifier, locked_at in LoginLock.objects.filter(
        identifier_hash__in=list(by_identifier)
    ).values_list("identifier_hash", "locked_at"):
        email = by_identifier[identifier]
        found[email] = min(locked_at, found.get(email, locked_at))
    return found


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


def purge_expired_locks(now: datetime | None = None) -> int:
    """Daily locks older than LOGIN_LOCK_MAX_AGE_S lift by themselves (hourly housekeeping):
    an unknown email's lock as a real one's, so how long one lasts tells nothing."""
    cutoff = (now or timezone.now()) - timedelta(seconds=settings.LOGIN_LOCK_MAX_AGE_S)
    deleted, _ = LoginLock.objects.filter(locked_at__lt=cutoff).delete()
    return deleted
