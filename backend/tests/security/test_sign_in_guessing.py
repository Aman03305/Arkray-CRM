"""Password guessing against one account is capped, not only slowed (adversarial review of
73ff1e3, L1: AUTH-2, AUTH-3, AUTH-4 and AUTH-7).

Before: every unrecognised browser shared one failure budget per account, so someone who
knew an email could try about 552 passwords a day for ever, keep the owner locked out of
every new browser, and lose nothing when the owner signed in from a new browser (that
forgave the whole budget). A failed sign-in of a real account also wrote two committed audit
rows that an unknown email didn't, a few milliseconds that told the two apart.

Now (identity.throttling, docs/authorization.md#sign-in-throttling):
- an unrecognised browser's budget is per account *and network* (an IPv4 /24, an IPv6 /64):
  a storm from one network doesn't refuse the right password from another;
- LOGIN_DAILY_FAILURE_CAP failures from unrecognised browsers in a day lock the account's
  password sign-in from every unrecognised browser (browsers that signed in before keep
  working) until the owner uses the emailed unlock link, resets the password or an
  administrator clears it; a success from an unrecognised browser no longer forgives the day;
- a failed sign-in does the same statements whether or not the email has an account;
- an email that isn't ASCII (a Kelvin sign "K" for a "k") never names an account.
"""

from __future__ import annotations

import re
import statistics
import time
from datetime import timedelta

import pytest
from django.conf import settings
from django.core import mail
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from arkray.audit.models import AuditEvent
from arkray.identity import services
from arkray.identity.emails import mask_email
from arkray.identity.models import AuthThrottleEvent, ThrottleKind, User
from arkray.identity.throttling import login_identifier
from tests.factories import DEFAULT_PASSWORD, UserFactory
from tests.helpers import drain_outbox, last_secret, signed_in, without_request_id

pytestmark = pytest.mark.django_db

LOGIN = "/api/v1/auth/login"
UNLOCK = "/api/v1/auth/sign-in-unlock"
UNLOCK_VERIFY = "/api/v1/auth/sign-in-unlock/verify"
RESET = "/api/v1/auth/password-reset"
RESET_CONFIRM = "/api/v1/auth/password-reset/confirm"
WRONG = "not-the-password-1"
NEW_PASSWORD = "a-brand-new-passphrase-42"
UNLOCK_LINK = re.compile(r"https?://[^\s/]+/unlock-sign-in/(?P<secret>[A-Za-z0-9_-]{43})(?=\s|$)")


def cap() -> int:
    return int(getattr(settings, "LOGIN_DAILY_FAILURE_CAP", 50))


def browser(address: str = "203.0.113.5") -> APIClient:
    """A browser that never signed in here (no trusted-device cookie), at `address`."""
    return APIClient(REMOTE_ADDR=address)


def login(client: APIClient, email: str, password: str = DEFAULT_PASSWORD):
    return client.post(LOGIN, {"email": email, "password": password}, format="json")


_networks = iter(range(1, 10_000))


def fresh_address() -> str:
    """An address in a network no other attempt of the test used (a new /24 each time)."""
    n = next(_networks)
    return f"10.{n // 250}.{n % 250}.7"


def fail_from_fresh_networks(email: str, times: int) -> None:
    for _ in range(times):
        response = login(browser(fresh_address()), email, WRONG)
        assert response.status_code == 400, response.content


def earlier_failures(email: str, count: int) -> None:
    """`count` failures from unrecognised browsers earlier today, outside the 15-minute
    window (so no per-network lockout is in force), each from its own network."""
    now = timezone.now()
    AuthThrottleEvent.objects.bulk_create(
        AuthThrottleEvent(
            kind=ThrottleKind.LOGIN_FAILURE,
            identifier_hash=login_identifier(email),
            device_id="",
            ip_address=f"198.18.{i % 250}.{1 + i // 250}",
            occurred_at=now - timedelta(hours=20) + timedelta(minutes=i),
        )
        for i in range(count)
    )


def trip(email: str) -> None:
    earlier_failures(email, cap() - 1)
    fail_from_fresh_networks(email, 1)


def unlock_secrets() -> list[str]:
    return [
        match["secret"]
        for message in mail.outbox
        for match in UNLOCK_LINK.finditer(str(message.body))
    ]


# --- AUTH-3: one network's failures don't refuse another network's right password ---------------
class TestNetworks:
    def test_a_failure_storm_from_one_network_doesnt_block_the_right_password_from_another(
        self, user_a
    ):
        attacker = browser("203.0.113.5")
        statuses = [login(attacker, user_a.email, WRONG).status_code for _ in range(8)]
        assert statuses[: settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD] == [400] * 5
        assert set(statuses[settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD :]) == {429}

        assert login(browser("198.51.100.7"), user_a.email).status_code == 200

    def test_a_network_shares_one_budget(self, user_a):
        """Rotating addresses inside one /24 buys nothing."""
        for host in range(1, settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD + 1):
            assert login(browser(f"203.0.113.{host}"), user_a.email, WRONG).status_code == 400
        assert login(browser("203.0.113.200"), user_a.email).status_code == 429

    def test_an_ipv6_network_is_its_64(self, user_a):
        for host in range(1, settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD + 1):
            response = login(browser(f"2001:db8:1:2::{host:x}"), user_a.email, WRONG)
            assert response.status_code == 400
        assert login(browser("2001:db8:1:2::beef"), user_a.email).status_code == 429
        assert login(browser("2001:db8:1:3::1"), user_a.email).status_code == 200

    def test_colleagues_typos_in_their_usual_browsers_dont_block_an_office(self, user_a):
        """L13 at the application: the per-address limit (password spraying) counts failures
        from unrecognised browsers only. Someone mistyping in a browser they signed in from
        before is the owner, not a sprayer; before, those counted toward blocking everyone's
        new browsers behind the same address."""
        office = "198.51.100.40"
        AuthThrottleEvent.objects.bulk_create(
            AuthThrottleEvent(
                kind=ThrottleKind.LOGIN_FAILURE,
                identifier_hash=login_identifier(f"colleague{i}@example.test"),
                device_id=f"{i:032x}",
                ip_address=office,
            )
            for i in range(settings.LOGIN_SOURCE_FAILURE_THRESHOLD)
        )
        assert login(browser(office), user_a.email).status_code == 200

    def test_spraying_from_one_address_is_still_blocked(self, user_a):
        """(Unrecognised browsers' failures from one address across many accounts: the
        durable per-address limit, as identity/tests/test_login.py has it too.)"""
        office = "198.51.100.41"
        AuthThrottleEvent.objects.bulk_create(
            AuthThrottleEvent(
                kind=ThrottleKind.LOGIN_FAILURE,
                identifier_hash=login_identifier(f"sprayed{i}@example.test"),
                ip_address=office,
            )
            for i in range(settings.LOGIN_SOURCE_FAILURE_THRESHOLD)
        )
        assert login(browser(office), user_a.email).status_code == 429
        assert login(browser("198.51.100.99"), user_a.email).status_code == 200


# --- AUTH-2: a daily cap, which a success from a new browser doesn't forgive ---------------------
class TestDailyCap:
    def test_failures_from_many_networks_lock_every_unrecognised_browser(self, user_a):
        """The regression test the review asked for: a day's failures trip the cap (here the
        last one through the API), whatever networks they came from."""
        trip(user_a.email)
        response = login(browser(fresh_address()), user_a.email)  # the right password
        assert response.status_code == 429
        body = response.json()["error"]
        assert body["code"] == "sign_in_locked"
        assert "emailed" in body["message"]
        assert int(response["Retry-After"]) > 60 * 60

    def test_a_browser_that_signed_in_before_keeps_working(self, user_a):
        owner = browser("198.51.100.20")
        assert login(owner, user_a.email).status_code == 200
        owner.post("/api/v1/auth/logout")
        trip(user_a.email)
        assert login(owner, user_a.email).status_code == 200

    def test_a_success_from_an_unrecognised_browser_doesnt_forgive_the_day(self, user_a):
        earlier_failures(user_a.email, cap() - 10)
        newcomer = browser("192.0.2.10")
        for _ in range(3):
            assert login(newcomer, user_a.email, WRONG).status_code == 400
        assert login(newcomer, user_a.email).status_code == 200  # the owner, on a new laptop
        fail_from_fresh_networks(user_a.email, 7)
        assert login(browser(fresh_address()), user_a.email).status_code == 429

    def test_below_the_cap_nothing_is_locked(self, user_a):
        earlier_failures(user_a.email, cap() - 2)
        fail_from_fresh_networks(user_a.email, 1)
        assert login(browser(fresh_address()), user_a.email).status_code == 200

    def test_unknown_emails_are_capped_exactly_alike(self, user_a):
        answers = []
        for email in (user_a.email, "nobody-here@example.test"):
            trip(email)
            response = login(browser(fresh_address()), email)
            answers.append(
                (response.status_code, without_request_id(response), "Retry-After" in response)
            )
        assert answers[0] == answers[1]
        drain_outbox()
        assert [message.to for message in mail.outbox] == [[user_a.email]]

    def test_refused_attempts_are_not_recorded(self, user_a):
        trip(user_a.email)
        before = AuthThrottleEvent.objects.count()
        for _ in range(5):
            login(browser(fresh_address()), user_a.email, WRONG)
        assert AuthThrottleEvent.objects.count() == before


class TestUnlocking:
    def test_the_owner_is_emailed_once_with_an_unlock_link(self, user_a):
        trip(user_a.email)
        drain_outbox()
        assert len(unlock_secrets()) == 1
        (message,) = mail.outbox
        assert message.to == [user_a.email]
        assert "failed sign-in" in message.body.lower()
        for _ in range(3):  # refused while locked: no further emails
            login(browser(fresh_address()), user_a.email, WRONG)
        drain_outbox()
        assert len(mail.outbox) == 1
        event = AuditEvent.objects.get(action="auth.sign_in_locked")
        assert (event.target_id, event.subject_user_id) == (str(user_a.pk), user_a.pk)

    def test_the_unlock_link_lifts_it_and_trusts_the_browser_that_opened_it(self, user_a):
        trip(user_a.email)
        drain_outbox()
        (secret,) = unlock_secrets()
        phone = browser("192.0.2.77")
        response = phone.post(UNLOCK, {"token": secret}, format="json")
        assert response.status_code == 204
        assert settings.LOGIN_DEVICE_COOKIE_NAME in response.cookies
        assert login(browser(fresh_address()), user_a.email).status_code == 200
        # Used once.
        again = browser().post(UNLOCK, {"token": secret}, format="json")
        assert again.json()["error"]["code"] == "invalid_token"
        assert AuditEvent.objects.filter(
            action="auth.sign_in_unlocked", metadata__by="email_link"
        ).exists()

    def test_the_browser_that_opened_the_link_stays_out_of_reach_of_a_new_storm(self, user_a):
        trip(user_a.email)
        drain_outbox()
        (secret,) = unlock_secrets()
        phone = browser("192.0.2.77")
        assert phone.post(UNLOCK, {"token": secret}, format="json").status_code == 204
        trip(user_a.email)
        assert login(browser(fresh_address()), user_a.email).status_code == 429
        assert login(phone, user_a.email).status_code == 200

    def test_a_password_reset_lifts_it(self, user_a):
        trip(user_a.email)
        mail.outbox.clear()
        browser().post(RESET, {"email": user_a.email}, format="json")
        drain_outbox()
        token = last_secret("reset-password")
        confirm = browser().post(
            RESET_CONFIRM, {"token": token, "new_password": NEW_PASSWORD}, format="json"
        )
        assert confirm.status_code == 204
        assert login(browser(fresh_address()), user_a.email, NEW_PASSWORD).status_code == 200

    def test_administrators_see_it_and_can_clear_it(self, admin, user_a):
        client = signed_in(admin)
        detail = f"/api/v1/admin/users/{user_a.pk}"
        assert client.get(detail).json()["sign_in_locked_at"] is None
        trip(user_a.email)
        assert client.get(detail).json()["sign_in_locked_at"] is not None
        listed = client.get("/api/v1/admin/users").json()["results"]
        assert {row["id"]: row["sign_in_locked_at"] is not None for row in listed}[str(user_a.pk)]

        cleared = client.post(f"{detail}/clear-sign-in-lock")
        assert cleared.status_code == 200
        assert cleared.json()["sign_in_locked_at"] is None
        assert login(browser(fresh_address()), user_a.email).status_code == 200
        event = AuditEvent.objects.get(action="auth.sign_in_unlocked")
        assert (event.actor_id, event.metadata["by"]) == (admin.pk, "administrator")

    def test_a_sales_user_cant_clear_one(self, user_a, user_b):
        trip(user_b.email)
        response = signed_in(user_a).post(f"/api/v1/admin/users/{user_b.pk}/clear-sign-in-lock")
        assert response.status_code == 403
        assert login(browser(fresh_address()), user_b.email).status_code == 429

    def test_both_show_in_the_security_events(self, admin, user_a):
        trip(user_a.email)
        drain_outbox()
        signed_in(admin).post(f"/api/v1/admin/users/{user_a.pk}/clear-sign-in-lock")
        actions = [
            row["action"]
            for row in signed_in(admin).get("/api/v1/admin/security-events").json()["results"]
        ]
        assert "auth.sign_in_locked" in actions
        assert "auth.sign_in_unlocked" in actions


# --- review round 1: unlock links (IDENTITY-1, IDENTITY-2) ---------------------------------------
class TestUnlockLinksReviewRound1:
    def test_an_unlock_link_never_takes_a_browsers_trust_from_another_account(self, user_a, user_b):
        """IDENTITY-1: one trusted-device cookie per browser, bound to one account. An insider's
        unlock link pressed in a colleague's everyday browser replaced the colleague's trust;
        the insider then tripped the colleague's cap, and that browser got `sign_in_locked`."""
        victim, insider = user_a, user_b
        everyday = browser("192.0.2.10")
        assert login(everyday, victim.email).status_code == 200
        trusted = everyday.cookies[settings.LOGIN_DEVICE_COOKIE_NAME].value
        everyday.post("/api/v1/auth/logout")
        trip(insider.email)
        drain_outbox()
        (secret,) = unlock_secrets()

        response = everyday.post(UNLOCK, {"token": secret}, format="json")
        assert response.status_code == 204
        assert settings.LOGIN_DEVICE_COOKIE_NAME not in response.cookies  # not replaced
        assert everyday.cookies[settings.LOGIN_DEVICE_COOKIE_NAME].value == trusted
        # The insider's lock was lifted all the same.
        assert login(browser(fresh_address()), insider.email).status_code == 200

        trip(victim.email)
        assert login(browser(fresh_address()), victim.email).status_code == 429
        assert login(everyday, victim.email).status_code == 200  # still trusted for the victim

    def test_a_browser_trusted_for_the_same_account_keeps_its_device(self, user_a):
        phone = browser("192.0.2.11")
        assert login(phone, user_a.email).status_code == 200
        phone.post("/api/v1/auth/logout")
        trip(user_a.email)
        drain_outbox()
        (secret,) = unlock_secrets()
        response = phone.post(UNLOCK, {"token": secret}, format="json")
        assert response.status_code == 204
        assert login(phone, user_a.email).status_code == 200

    def test_the_page_can_name_the_account_masked_without_using_the_link(self, user_a):
        trip(user_a.email)
        drain_outbox()
        (secret,) = unlock_secrets()
        preview = browser().post(UNLOCK_VERIFY, {"token": secret}, format="json")
        assert preview.status_code == 200
        assert preview.json() == {"email": mask_email(user_a.email)}
        assert user_a.email not in preview.content.decode()
        # Nothing used up: the link still works once.
        assert browser().post(UNLOCK, {"token": secret}, format="json").status_code == 204
        stale = browser().post(UNLOCK_VERIFY, {"token": secret}, format="json")
        assert (stale.status_code, stale.json()["error"]["code"]) == (400, "invalid_token")


def _admin_changes_the_email(admin, user):
    services.change_user_email(
        actor_id=admin.pk,
        user_id=user.pk,
        version=User.objects.get(pk=user.pk).version,
        email="new.mailbox@example.test",
    )


def _admin_sets_a_password(admin, user):
    services.set_user_password(
        actor_id=admin.pk,
        user_id=user.pk,
        version=User.objects.get(pk=user.pk).version,
        new_password=NEW_PASSWORD,
    )


def _owner_changes_the_password(admin, user, owner):
    changed = owner.post(
        "/api/v1/auth/password/change",
        {"current_password": DEFAULT_PASSWORD, "new_password": NEW_PASSWORD},
        format="json",
    )
    assert changed.status_code == 204, changed.content


def _owner_resets_the_password(admin, user):
    mail.outbox.clear()
    browser().post(RESET, {"email": user.email}, format="json")
    drain_outbox()
    confirm = browser().post(
        RESET_CONFIRM,
        {"token": last_secret("reset-password"), "new_password": NEW_PASSWORD},
        format="json",
    )
    assert confirm.status_code == 204, confirm.content


def _admin_deactivates(admin, user):
    services.deactivate_user(actor_id=admin.pk, user_id=user.pk)


@pytest.mark.parametrize(
    "change",
    [
        _admin_changes_the_email,
        _admin_sets_a_password,
        _owner_changes_the_password,
        _owner_resets_the_password,
        _admin_deactivates,
    ],
    ids=["admin-email", "admin-password", "own-password", "reset", "deactivate"],
)
def test_a_credential_change_revokes_a_pending_unlock_link(admin, user_a, change):
    """IDENTITY-2: the link was revoked by none of these, and the trust it grants is computed
    when it is used, so a stale link trusted a browser for the account's NEW credentials."""
    owner = browser("192.0.2.12")
    assert login(owner, user_a.email).status_code == 200  # a trusted, signed-in browser
    trip(user_a.email)
    drain_outbox()
    (secret,) = unlock_secrets()
    if change is _owner_changes_the_password:
        change(admin, user_a, owner)
    else:
        change(admin, user_a)
    stale = browser("198.51.100.77").post(UNLOCK, {"token": secret}, format="json")
    assert (stale.status_code, stale.json()["error"]["code"]) == (400, "invalid_token")


# --- AUTH-4: the same work whether or not the email has an account --------------------------------
_TABLE = re.compile(r'\b(?:FROM|INTO|UPDATE)\s+"(\w+)"', re.IGNORECASE)


def _shape(sql: str) -> tuple[str, str]:
    """A statement as (verb, first table), literals and savepoint names left out. A savepoint
    released or rolled back are one shape: what was written is the same either way."""
    words = sql.split()
    verb = words[0].upper()
    if verb in {"RELEASE", "ROLLBACK"}:
        return ("END_SAVEPOINT", "")
    if verb == "SAVEPOINT":
        return ("SAVEPOINT", "")
    table = _TABLE.search(sql)
    return (verb, table.group(1) if table else "")


def statements_of(email: str) -> list[tuple[str, str]]:
    with CaptureQueriesContext(connection) as queries:
        assert login(browser(fresh_address()), email, WRONG).status_code == 400
    return [_shape(query["sql"]) for query in queries]


class TestEqualWork:
    def test_an_unknown_email_and_a_wrong_password_run_the_same_statements(self, user_a):
        """R135 was accepted on "two INSERTs next to the password hash"; the review measured
        them (+3 to +7 ms) through network jitter with a few hundred samples per email."""
        UserFactory(email="deactivated@example.test", is_active=False)
        known = statements_of(user_a.email)
        assert known == statements_of("nobody-at-all@example.test")
        assert known == statements_of("deactivated@example.test")
        # The real account's failure is still audited (and nothing for the unknown one).
        events = AuditEvent.objects.filter(action="auth.login_failed")
        assert sorted(e.metadata["reason"] for e in events) == [
            "account_inactive",
            "wrong_password",
        ]


def _paired_gaps(accounts: list[User], round_: int) -> tuple[list[float], list[float]]:
    """One wrong-password sign-in of each account next to one of an unknown email, from
    fresh browsers, the order alternating: (known minus unknown per pair, unknown's times)."""
    gaps: list[float] = []
    durations: list[float] = []
    for i, account in enumerate(accounts):
        timed: dict[str, float] = {}
        emails = [("known", account.email), ("unknown", f"ghost-{round_}-{i}@example.test")]
        for kind, email in emails if (round_ + i) % 2 else reversed(emails):
            client = browser(fresh_address())
            started = time.perf_counter()
            assert login(client, email, WRONG).status_code == 400
            timed[kind] = time.perf_counter() - started
        gaps.append(timed["known"] - timed["unknown"])
        durations.append(timed["unknown"])
    return gaps, durations


# The largest median paired gap accepted. The original gap (two committed audit inserts) was 3
# to 7 ms through a network (the review) and 9 to 11 ms here; after the fix the reviewer
# measured -0.22 ms, 95 % CI [-0.91, +0.79] ms over 250 pairs (review round 1).
GAP_LIMIT_S = 0.0015
PAIRS_PER_TRY = 200


@pytest.mark.django_db(transaction=True)
def test_timing_doesnt_tell_existing_emails_apart():
    """Statistical but robust: paired, interleaved samples (each existing-account attempt next
    to an unknown-email one, the order alternating, so the machine's load drifts out), and the
    median of the paired differences within GAP_LIMIT_S, an absolute limit (review round 1:
    a 15 % one was 4 to 7.5 ms on a loaded laptop, wide enough for the original gap). After a
    warm-up; up to three tries of PAIRS_PER_TRY pairs, since a laptop running other work can
    be noisy for a while (the sample median's spread is about a millisecond here, so three
    misses in a row are rare; the original gap misses every time)."""
    accounts = [UserFactory() for _ in range(PAIRS_PER_TRY // 4)]  # a handful of attempts each
    _paired_gaps(accounts[:10], -1)  # warm-up: not counted
    AuthThrottleEvent.objects.all().delete()
    results = []
    for attempt in range(3):
        gaps, durations = [], []
        for round_ in range(4):
            round_gaps, round_durations = _paired_gaps(accounts, attempt * 4 + round_)
            gaps += round_gaps
            durations += round_durations
        gap, typical = statistics.median(gaps), statistics.median(durations)
        results.append((round(gap * 1000, 2), round(typical * 1000, 2)))
        if abs(gap) < GAP_LIMIT_S:
            return
        # Forget this try's failures (outside the daily cap's concern here).
        AuthThrottleEvent.objects.all().delete()
    pytest.fail(f"median paired gap (ms) and typical attempt (ms) per try: {results}")


# --- AUTH-7: an email that isn't ASCII never names an account -----------------------------------
KELVIN = chr(0x212A)  # KELVIN SIGN: str.lower() makes it an ASCII "k"


class TestNonAsciiEmails:
    @pytest.fixture
    def kelvin(self):
        return UserFactory(email="kelvin@example.test")

    def test_a_kelvin_sign_alias_doesnt_sign_in(self, kelvin):
        alias = f"{KELVIN}elvin@example.test"
        response = login(browser(), alias)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_credentials"

    def test_the_alias_spends_its_own_throttle_slot_not_the_accounts(self, kelvin):
        alias = f"{KELVIN}elvin@example.test"
        attacker = browser("203.0.113.5")
        for _ in range(settings.LOGIN_ACCOUNT_FAILURE_THRESHOLD):
            assert login(attacker, alias).status_code == 400
        assert (
            AuthThrottleEvent.objects.filter(identifier_hash=login_identifier(kelvin.email)).count()
            == 0
        )
        assert login(attacker, kelvin.email).status_code == 200

    def test_a_password_reset_for_the_alias_sends_nothing(self, kelvin):
        response = browser().post(RESET, {"email": f"{KELVIN}elvin@example.test"}, format="json")
        assert response.status_code == 202
        drain_outbox()
        assert mail.outbox == []

    def test_an_invitation_or_email_change_to_one_is_refused(self, admin, user_a):
        client = signed_in(admin)
        invite = client.post(
            "/api/v1/admin/users",
            {"first_name": "K", "email": f"{KELVIN}im@example.test", "role": "sales_user"},
            format="json",
        )
        assert invite.status_code == 400
        change = client.post(
            f"/api/v1/admin/users/{user_a.pk}/change-email",
            {"email": f"{KELVIN}im@example.test", "version": user_a.version},
            format="json",
        )
        assert change.status_code == 400
