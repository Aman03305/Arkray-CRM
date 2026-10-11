"""Rotating the erasure ledger's key (final production audit ARCH-5; core.ledger.rotate_key,
docs/runbooks.md#rotate-the-erasure-ledger-key).

Rotation re-signs nothing and loses nothing: ERASURE_LEDGER_VERIFY_KEYS keeps the old key
accepted while a `key_rotated` entry, signed with the new key, anchors every entry before it.
From then on the old key can't add entries, and once it is removed from the configuration
the entries it signed stay verifiable through the anchor (a restore included).
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.db import transaction

from arkray.core import ledger
from arkray.core.ledger_gate import GATE
from arkray.core.models import LedgerState
from arkray.leads.models import Lead
from arkray.privacy import reconcile, replay, services
from arkray.privacy.services import ERASED
from tests.architecture.test_production_settings import BASE_ENV, load_production_settings
from tests.factories import LeadFactory
from tests.helpers import signed_in

pytestmark = pytest.mark.django_db

OLD = "ledger-old-key-" + "o" * 32
NEW = "ledger-new-key-" + "n" * 32


@pytest.fixture
def ledger_dir(settings, tmp_path):
    directory = tmp_path / "ledger"
    directory.mkdir()
    settings.ERASURE_LEDGER_URL = directory.as_uri()
    settings.ERASURE_LEDGER_KEY = OLD
    settings.ERASURE_LEDGER_VERIFY_KEYS = []
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 0
    GATE.reset()
    yield directory
    GATE.reset()


def files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


def entries(directory: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in files(directory)]


def erase(admin, owner):
    lead = LeadFactory(owner=owner)
    services.erase(lead.pk, operator_id=admin.pk)
    return lead


def use(settings, key: str, *verify: str) -> None:
    settings.ERASURE_LEDGER_KEY = key
    settings.ERASURE_LEDGER_VERIFY_KEYS = list(verify)
    GATE.reset()


@pytest.fixture
def rotated(ledger_dir, settings, admin, user_a):
    """Two entries under the old key, the rotation, one entry under the new key."""
    before = [erase(admin, user_a), erase(admin, user_a)]
    use(settings, NEW, OLD)  # every process signs with the new key, accepts the old
    entry = ledger.rotate_key()
    assert entry is not None
    assert entry.seq == 3
    assert entry.kind == ledger.KEY_ROTATED
    after = erase(admin, user_a)
    return {"before": before, "after": after}


def test_rotation_keeps_every_entry_and_names_the_key_by_id_only(rotated, ledger_dir, settings):
    found = entries(ledger_dir)
    assert [e["kind"] for e in found] == [
        "lead_erased",
        "lead_erased",
        "key_rotated",
        "lead_erased",
    ]
    assert found[2]["subject"] == ledger.key_id(NEW) != NEW
    assert found[2]["anchor"]
    raw = "".join(p.read_text() for p in files(ledger_dir))
    assert NEW not in raw
    assert OLD not in raw
    assert [e.seq for e in ledger.verify()] == [1, 2, 3, 4]
    assert ledger.applied()[0] == 4
    assert GATE.status() == "ok"


def test_the_old_key_can_be_retired_after_the_rotation(rotated, settings, user_a):
    use(settings, NEW)  # old key removed everywhere
    assert [e.seq for e in ledger.verify()] == [1, 2, 3, 4]
    assert GATE.status() == "ok"
    assert signed_in(user_a).get("/api/v1/auth/me").status_code == 200


def test_but_not_before_it(ledger_dir, settings, admin, user_a):
    erase(admin, user_a)
    use(settings, NEW, OLD)
    erase(admin, user_a)  # signed with the new key: both accepted during the transition
    assert [e.seq for e in ledger.verify()] == [1, 2]
    use(settings, NEW)  # the old key dropped without a rotation: entry 1 can't be checked
    with pytest.raises(ledger.LedgerTampered, match="entry 1"):
        ledger.verify()
    assert GATE.status() == "tampered"


def _forge(directory: Path, key: str) -> None:
    """Append an entry signed with `key`, linked to the head (as an attacker holding it)."""
    last = entries(directory)[-1]
    draft = ledger.Entry(
        seq=last["seq"] + 1,
        kind="lead_erased",
        subject="00000000-0000-0000-0000-000000000001",
        at=last["at"],
        prev=last["mac"],
        mac="",
    )
    forged = ledger.Entry(**{**draft.content(), "mac": ledger._mac(draft.content(), key)})
    (directory / f"{forged.seq:012d}.json").write_bytes(forged.as_json())


def test_a_retired_key_still_configured_cannot_add_entries(rotated, ledger_dir, settings):
    _forge(ledger_dir, OLD)
    with pytest.raises(ledger.LedgerTampered, match="retired key"):
        ledger.verify()


def test_a_retired_key_no_longer_configured_cannot_add_entries(rotated, ledger_dir, settings):
    use(settings, NEW)
    _forge(ledger_dir, OLD)
    with pytest.raises(ledger.LedgerTampered, match="entry 5"):
        ledger.verify()
    assert GATE.status() == "tampered"


def test_a_retired_key_cannot_bring_itself_back_through_a_rotation(
    rotated, ledger_dir, settings, user_a
):
    """Review P2: the old key, leaked and still accepted during the transition, signs a
    `key_rotated` entry naming itself (its anchor is public), then an erasure."""
    _, state = ledger.verified()
    last = entries(ledger_dir)[-1]
    draft = ledger.Entry(
        seq=last["seq"] + 1,
        kind=ledger.KEY_ROTATED,
        subject=ledger.key_id(OLD),
        at=last["at"],
        prev=last["mac"],
        mac="",
        anchor=state.digest,
    )
    rotation = ledger.Entry(**{**draft.content(), "mac": ledger._mac(draft.content(), OLD)})
    (ledger_dir / f"{rotation.seq:012d}.json").write_bytes(rotation.as_json())
    _forge(ledger_dir, OLD)
    with pytest.raises(ledger.LedgerTampered, match="retired key"):
        ledger.verify()
    assert GATE.status() == "tampered"
    assert reconcile.reconcile() == "refused"  # nothing applied


def test_an_entry_altered_before_the_rotation_breaks_the_anchor(rotated, ledger_dir, settings):
    use(settings, NEW)
    first = files(ledger_dir)[0]
    data = json.loads(first.read_text())
    data["subject"] = "00000000-0000-0000-0000-000000000000"
    first.write_text(json.dumps(data))
    with pytest.raises(ledger.LedgerTampered):
        ledger.verify()


def test_a_forged_rotation_is_refused(ledger_dir, settings, admin, user_a):
    erase(admin, user_a)
    last = entries(ledger_dir)[-1]
    attacker = "ledger-attacker-key-" + "a" * 32
    draft = ledger.Entry(
        seq=2,
        kind=ledger.KEY_ROTATED,
        subject=ledger.key_id(attacker),
        at=last["at"],
        prev=last["mac"],
        mac="",
        anchor="0" * 64,
    )
    forged = ledger.Entry(**{**draft.content(), "mac": ledger._mac(draft.content(), attacker)})
    (ledger_dir / "000000000002.json").write_bytes(forged.as_json())
    with pytest.raises(ledger.LedgerTampered):
        ledger.verify()


def test_rotating_again_is_harmless(rotated, ledger_dir):
    assert ledger.rotate_key() is None
    assert len(files(ledger_dir)) == 4


def test_a_second_rotation_retires_the_first_new_key(rotated, ledger_dir, settings, admin, user_a):
    newer = "ledger-newer-key-" + "w" * 32
    use(settings, newer, NEW)
    erase(admin, user_a)
    assert ledger.rotate_key() is not None
    use(settings, newer)
    assert [e.seq for e in ledger.verify()] == [1, 2, 3, 4, 5, 6]
    _forge(ledger_dir, NEW)
    with pytest.raises(ledger.LedgerTampered):
        ledger.verify()


def test_a_rotation_is_refused_over_an_unverifiable_ledger(ledger_dir, settings, admin, user_a):
    erase(admin, user_a)
    use(settings, NEW)  # the old key forgotten: entry 1 can't be verified, nor anchored
    with pytest.raises(ledger.LedgerTampered):
        ledger.rotate_key()
    assert len(files(ledger_dir)) == 1


class _Crash(Exception):
    pass


def test_an_interrupted_rotation_is_reconciled(ledger_dir, settings, admin, user_a):
    """The rotation's entry written, its transaction lost: the database is one entry behind,
    which the automatic reconciliation applies (nothing to do for a rotation)."""
    erase(admin, user_a)
    use(settings, NEW, OLD)

    def rotate_then_crash() -> None:
        with transaction.atomic():
            ledger.rotate_key()
            raise _Crash

    with pytest.raises(_Crash):
        rotate_then_crash()
    assert entries(ledger_dir)[-1]["kind"] == ledger.KEY_ROTATED
    assert GATE.status() == "behind"
    assert reconcile.reconcile() == "reconciled"
    GATE.reset()
    assert GATE.status() == "ok"
    assert ledger.rotate_key() is None  # already anchored


def test_a_restore_replays_entries_signed_with_both_keys(rotated, settings, admin):
    """A backup from before any of it: leads back, state 0. Only the new key is configured."""
    use(settings, NEW)
    for lead in [*rotated["before"], rotated["after"]]:
        Lead.objects.filter(pk=lead.pk).update(first_name="Restored")
    LedgerState.objects.all().delete()
    GATE.reset()
    assert GATE.status() == "behind"
    report = replay.replay(operator=admin)
    assert report.complete, report.as_dict()
    assert report.counts() == {"reapplied": 3, "already_applied": 1}
    for lead in [*rotated["before"], rotated["after"]]:
        assert Lead.objects.get(pk=lead.pk).first_name == ERASED
    GATE.reset()
    assert GATE.status() == "ok"


def test_the_command_checks_then_rotates(ledger_dir, settings, admin, user_a):
    erase(admin, user_a)
    use(settings, NEW, OLD)
    out = StringIO()
    call_command("rotate_erasure_ledger_key", "--check", stdout=out)
    assert "1 entries verified" in out.getvalue()
    assert len(files(ledger_dir)) == 1
    out = StringIO()
    call_command("rotate_erasure_ledger_key", stdout=out)
    assert "Entry 2 anchors the ledger" in out.getvalue()
    assert NEW not in out.getvalue()
    out = StringIO()
    call_command("rotate_erasure_ledger_key", stdout=out)
    assert "Already anchored" in out.getvalue()


@pytest.mark.parametrize(
    ("keys", "accepted"),
    [
        ("v" * 48, True),
        (f"{'v' * 48},{'w' * 40}", True),
        ("short", False),
        ("l" * 48, False),  # the signing key itself
    ],
    ids=["one", "two", "short", "same-as-signing"],
)
def test_production_settings_check_the_verification_keys(keys, accepted):
    result = load_production_settings(
        "settings.ERASURE_LEDGER_VERIFY_KEYS", **{**BASE_ENV, "ERASURE_LEDGER_VERIFY_KEYS": keys}
    )
    assert (result.returncode == 0) is accepted, result.stderr[-1000:]
    if not accepted:
        assert "ERASURE_LEDGER_VERIFY_KEYS" in result.stderr
