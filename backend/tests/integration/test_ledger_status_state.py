"""`manage.py ledger_status` at any ledger size (security review M3; the Celery containers'
health check, every minute).

Each probe is a new process, and each verified the whole chain from scratch: one GET per entry
on S3, every minute, in every Celery container. As the ledger grew, the probe outlasted its
30 s timeout and healthy workers turned unhealthy. A probe now keeps the chain it verified
(a file in the container's temporary directory, signed with the ledger key) and the next one
reads only the entries added since; the whole chain is verified again at least every
`FULL_CHECK_EVERY_S`, a bounded slice per probe, so an entry altered or removed after it was
verified is still caught. A state file it can't trust is ignored (the whole chain again).
"""

from __future__ import annotations

import json
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from arkray.core import ledger
from arkray.core.ledger_gate import GATE
from arkray.core.management.commands import ledger_status
from arkray.privacy import services
from tests.factories import LeadFactory

pytestmark = pytest.mark.django_db

KEY = "ledger-test-key-" + "x" * 32


@pytest.fixture
def ledger_dir(settings, tmp_path, monkeypatch):
    directory = tmp_path / "ledger"
    directory.mkdir()
    state = tmp_path / "tmp"
    state.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(state))
    settings.ERASURE_LEDGER_URL = directory.as_uri()
    settings.ERASURE_LEDGER_KEY = KEY
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 0
    GATE.reset()
    yield directory
    GATE.reset()


def erase(admin, owner):
    services.erase(LeadFactory(owner=owner).pk, operator_id=admin.pk)


def probe() -> str:
    """One health check: a new process (a fresh gate)."""
    GATE.reset()
    out = StringIO()
    call_command("ledger_status", stdout=out)
    return out.getvalue()


@pytest.fixture
def reads(monkeypatch):
    """Where each read of the ledger's entries started (`after`)."""
    found: list[int] = []
    store = ledger.FileStore
    real = store.read

    def counting(self, after=0, *args, **kwargs):
        found.append(after)
        return real(self, after, *args, **kwargs)

    monkeypatch.setattr(store, "read", counting)
    return found


def test_a_probe_after_the_first_reads_only_the_new_entries(ledger_dir, admin, user_a, reads):
    for _ in range(3):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    assert reads == [0]  # the first probe: the whole chain
    erase(admin, user_a)
    reads.clear()
    assert probe() == "erasure_ledger_gate=ok\n"
    assert reads == [3]  # only entry 4
    reads.clear()
    assert probe() == "erasure_ledger_gate=ok\n"
    assert reads == []  # nothing new: the head only


def test_the_whole_chain_is_verified_again_after_an_hour(ledger_dir, admin, user_a, monkeypatch):
    monkeypatch.setattr(ledger, "CHECKPOINT_EVERY", 2)  # with checkpoints above the change
    for _ in range(3):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    first = ledger_dir / "000000000001.json"
    data = json.loads(first.read_text())
    data["subject"] = "00000000-0000-0000-0000-000000000000"
    first.write_text(json.dumps(data))  # altered after it was verified
    assert probe() == "erasure_ledger_gate=ok\n"  # within the hour: not read again
    now = ledger_status._now()
    monkeypatch.setattr(ledger_status, "_now", lambda: now + ledger_status.FULL_CHECK_EVERY_S + 1)
    for _ in range(4):  # and stays so, probe after probe (review round 2, LEDGER2-1)
        with pytest.raises(CommandError, match="tampered"):
            probe()


def test_a_long_chain_is_verified_again_a_slice_per_probe(ledger_dir, admin, user_a, monkeypatch):
    for _ in range(5):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    monkeypatch.setattr(ledger_status, "AUDIT_ENTRIES_PER_PROBE", 2)
    now = ledger_status._now()
    monkeypatch.setattr(ledger_status, "_now", lambda: now + ledger_status.FULL_CHECK_EVERY_S + 1)
    assert probe() == "erasure_ledger_gate=ok\n"  # entries 1-2
    assert _saved()["audit"]["seq"] == 2
    assert probe() == "erasure_ledger_gate=ok\n"  # 3-4
    assert probe() == "erasure_ledger_gate=ok\n"  # 5: the pass is complete
    saved = _saved()
    assert saved["audit"] is None
    assert saved["full_at"] == pytest.approx(now + ledger_status.FULL_CHECK_EVERY_S + 1)


def _state_file() -> Path:
    (path,) = Path(tempfile.gettempdir()).glob("arkray-ledger-status-*.json")
    return path


def _saved() -> dict[str, Any]:
    return dict(json.loads(_state_file().read_text()))


def test_a_state_file_it_cannot_trust_is_ignored(ledger_dir, admin, user_a, reads):
    for _ in range(3):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    path = _state_file()
    saved = json.loads(path.read_text())
    saved["verified"]["seq"] = 2  # forged: "verified up to 2" (the MAC no longer matches)
    path.write_text(json.dumps(saved))
    reads.clear()
    assert probe() == "erasure_ledger_gate=ok\n"
    assert reads == [0]  # the whole chain again
    path.write_text("not json")
    reads.clear()
    assert probe() == "erasure_ledger_gate=ok\n"
    assert reads == [0]


def test_a_rolled_back_ledger_closes_the_next_probe(ledger_dir, admin, user_a):
    for _ in range(3):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    (ledger_dir / "000000000003.json").unlink()
    with pytest.raises(CommandError, match="ahead"):
        probe()


def test_another_ledger_never_uses_this_ones_state(ledger_dir, admin, user_a, settings, tmp_path):
    for _ in range(2):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    other = tmp_path / "other"
    other.mkdir()
    settings.ERASURE_LEDGER_URL = other.as_uri()
    with pytest.raises(CommandError, match="ahead"):
        probe()


def test_without_a_ledger_nothing_is_kept(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    settings.ERASURE_LEDGER_URL = ""
    try:
        assert probe() == "erasure_ledger_gate=disabled\n"
    finally:
        GATE.reset()
    assert list(tmp_path.glob("arkray-ledger-status-*")) == []


# --- review round 1 ------------------------------------------------------------------------------
@pytest.fixture
def sliced_reads(monkeypatch):
    """Every read of the ledger's entries, as (after, limit)."""
    found: list[tuple[int, int | None]] = []
    real = ledger.FileStore.read

    def counting(self, after=0, limit=None):
        found.append((after, limit))
        return real(self, after, limit)

    monkeypatch.setattr(ledger.FileStore, "read", counting)
    return found


def _hour_later(monkeypatch) -> None:
    now = ledger_status._now()
    monkeypatch.setattr(ledger_status, "_now", lambda: now + ledger_status.FULL_CHECK_EVERY_S + 1)


def test_a_first_probe_verifies_a_slice_at_a_time(
    ledger_dir, admin, user_a, monkeypatch, sliced_reads
):
    """LEDGER-2 (b): the first probe of a container verified the whole chain at once."""
    for _ in range(5):
        erase(admin, user_a)
    monkeypatch.setattr(ledger_status, "AUDIT_ENTRIES_PER_PROBE", 2)
    for _ in range(2):
        with pytest.raises(CommandError, match="verifying"):
            probe()
    assert probe() == "erasure_ledger_gate=ok\n"
    assert all(limit is not None and limit <= 2 for _, limit in sliced_reads), sliced_reads


def test_after_a_key_rotation_the_pass_stays_sliced(
    ledger_dir, admin, user_a, settings, monkeypatch
):
    """LEDGER-2 (c): with the old key dropped, the pass's first slice couldn't vouch for the
    entries the rotation anchors, and the rest of the chain was read in one probe."""
    old, new = "ledger-old-key-" + "o" * 32, "ledger-new-key-" + "n" * 32
    settings.ERASURE_LEDGER_KEY = old
    for _ in range(8):
        erase(admin, user_a)
    settings.ERASURE_LEDGER_KEY, settings.ERASURE_LEDGER_VERIFY_KEYS = new, [old]
    assert ledger.rotate_key() is not None
    settings.ERASURE_LEDGER_VERIFY_KEYS = []
    for _ in range(2):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    reads: list[tuple[int, int | None]] = []
    real = ledger.FileStore.read

    def counting(self, after=0, limit=None):
        reads.append((after, limit))
        return real(self, after, limit)

    monkeypatch.setattr(ledger.FileStore, "read", counting)
    monkeypatch.setattr(ledger_status, "AUDIT_ENTRIES_PER_PROBE", 2)
    _hour_later(monkeypatch)
    for _ in range(6):
        assert probe() == "erasure_ledger_gate=ok\n"
        if _saved()["audit"] is None:
            break
    assert _saved()["audit"] is None  # the pass completed
    assert all(limit is not None and limit <= 2 for _, limit in reads), reads


def test_a_pass_completes_while_the_ledger_grows(ledger_dir, admin, user_a, monkeypatch):
    """Info (review): a pass chased the head and never ended if the ledger grew faster than a
    slice per probe. It now ends at the entry it started from."""
    for _ in range(5):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    monkeypatch.setattr(ledger_status, "AUDIT_ENTRIES_PER_PROBE", 2)
    _hour_later(monkeypatch)
    for _ in range(3):
        assert probe() == "erasure_ledger_gate=ok\n"
        for _ in range(3):
            erase(admin, user_a)  # three more entries a probe: faster than the pass
    assert _saved()["audit"] is None


def test_a_container_started_from_a_checkpoint_still_verifies_everything(
    ledger_dir, admin, user_a, monkeypatch
):
    """A first probe starts from the newest signed checkpoint (bounded at any size); the
    whole chain is still verified from scratch by the next ones, so an entry altered after
    the checkpoint was written is caught."""
    monkeypatch.setattr(ledger, "CHECKPOINT_EVERY", 2)
    for _ in range(4):
        erase(admin, user_a)
    ledger.verified_state()  # some process verified it: checkpoints at 2 and 4
    assert sorted(p.name.split(".")[0] for p in (ledger_dir / "checkpoints").iterdir()) == [
        "000000000002",
        "000000000004",
    ]
    first = ledger_dir / "000000000001.json"
    data = json.loads(first.read_text())
    data["subject"] = "00000000-0000-0000-0000-000000000000"
    first.write_text(json.dumps(data))
    assert probe() == "erasure_ledger_gate=ok\n"  # from checkpoint 4
    for _ in range(4):  # the pass from scratch, due at once, then sticky (review round 2)
        with pytest.raises(CommandError, match="tampered"):
            probe()


def test_a_file_planted_at_a_temporary_name_is_never_written_through(ledger_dir, admin, user_a):
    """Info (review): the state was written through a predictable temporary name (O_CREAT
    without O_EXCL): a file (or link) planted there was truncated and written."""
    import os

    erase(admin, user_a)
    path = ledger_status._path()
    planted = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    planted.write_text("PLANTED")
    assert probe() == "erasure_ledger_gate=ok\n"
    assert planted.read_text() == "PLANTED"
    assert _saved()["verified"]["seq"] == 1


# --- review round 2 ------------------------------------------------------------------------------
def _alter(directory: Path, seq: int) -> None:
    path = directory / f"{seq:012d}.json"
    data = json.loads(path.read_text())
    data["subject"] = "00000000-0000-0000-0000-000000000000"
    path.write_text(json.dumps(data))


def _failures(count: int) -> list[str]:
    found = []
    for _ in range(count):
        try:
            found.append(probe().strip())
        except CommandError as exc:
            found.append(str(exc))
    return found


def test_a_tampered_verdict_stays_until_the_whole_chain_verifies_again(
    ledger_dir, admin, user_a, monkeypatch
):
    """LEDGER2-1: with checkpoints, the probe after a tampered verdict started again from a
    checkpoint above the altered entry and answered ok: ok ok ok FAIL ok ok ok ok FAIL, never
    three failures in a row, so the container never turned unhealthy."""
    monkeypatch.setattr(ledger, "CHECKPOINT_EVERY", 2)
    for _ in range(6):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"  # checkpoints at 2, 4 and 6 kept
    _alter(ledger_dir, 3)
    _hour_later(monkeypatch)
    verdicts = _failures(5)
    assert all("tampered" in v for v in verdicts), verdicts
    # A new container (no state file): from a checkpoint, then the pass from scratch, which
    # stays failed.
    _state_file().unlink()
    verdicts = _failures(5)
    assert verdicts[0] == "erasure_ledger_gate=ok"
    assert all("tampered" in v for v in verdicts[1:]), verdicts


def test_the_tampered_verdict_is_logged_for_alerting(
    ledger_dir, admin, user_a, monkeypatch, caplog
):
    import logging

    for _ in range(3):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    _alter(ledger_dir, 1)
    _hour_later(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="arkray.core.ledger_gate"):
        _failures(3)
    flagged = [r for r in caplog.records if r.getMessage() == "erasure_ledger_tampered"]
    assert len(flagged) == 3  # every probe, not only the first
    assert all(getattr(r, "security", False) for r in flagged)


def test_unreadable_checkpoints_count_as_none(ledger_dir, admin, user_a, monkeypatch, caplog):
    """LEDGER2-7: with the checkpoints denied (an IAM policy without that prefix) every probe
    answered "unavailable" and prime failed."""
    import logging

    monkeypatch.setattr(ledger, "CHECKPOINT_EVERY", 2)
    for _ in range(4):
        erase(admin, user_a)

    def denied(self, *args, **kwargs):
        raise ledger.LedgerUnavailable("access denied")

    monkeypatch.setattr(ledger.FileStore, "get_checkpoint", denied)
    monkeypatch.setattr(ledger.FileStore, "put_checkpoint", denied)
    with caplog.at_level(logging.WARNING, logger="arkray.core.ledger"):
        assert probe() == "erasure_ledger_gate=ok\n"
        assert ledger.checkpoint() is None
        GATE.reset()
        from arkray.core.ledger_gate import prime

        prime()
    state = GATE.verified_state()
    assert state is not None
    assert state.seq == 4
    assert any(r.getMessage() == "erasure_ledger_checkpoint_unavailable" for r in caplog.records)


# --- review round 3 ------------------------------------------------------------------------------
def test_an_operator_can_verify_the_whole_chain_from_scratch(
    ledger_dir, admin, user_a, monkeypatch
):
    """Info I-2: the sticky verdict lives in the container's tmpfs; after a restart the first
    probe trusts the newest checkpoint, and between hourly passes a probe reads nothing below
    its kept state. `--from-scratch` verifies every entry, at once."""
    monkeypatch.setattr(ledger, "CHECKPOINT_EVERY", 2)
    for _ in range(6):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    assert probe() == "erasure_ledger_gate=ok\n"  # the whole chain: next pass in an hour
    kept = ledger_status._load()
    assert kept is not None
    assert ledger_status._now() - kept["full_at"] < 60
    _alter(ledger_dir, 3)
    assert probe() == "erasure_ledger_gate=ok\n"  # a probe reads nothing below its state
    GATE.reset()
    with pytest.raises(CommandError, match="tampered"):
        call_command("ledger_status", "--from-scratch", stdout=StringIO())
    with pytest.raises(CommandError, match="tampered"):
        probe()  # and sticky from there


def test_an_operator_s_pass_over_a_sound_chain_clears_the_verdict(
    ledger_dir, admin, user_a, monkeypatch
):
    """Info I-2: `--from-scratch` over a sound chain answers ok and clears a tampered
    verdict (e.g. once the right ledger is restored); while it runs, the probes never see a
    "tampered" it hasn't found."""
    monkeypatch.setattr(ledger_status, "_SLICE", 1)  # many slices: each could be kept
    for _ in range(4):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    saves: list[bool] = []
    real = ledger_status._save

    def watching(*args, **kwargs):
        saves.append(bool(kwargs.get("tampered")))
        return real(*args, **kwargs)

    monkeypatch.setattr(ledger_status, "_save", watching)
    GATE.reset()
    out = StringIO()
    call_command("ledger_status", "--from-scratch", stdout=out)
    assert out.getvalue() == "erasure_ledger_gate=ok\n"
    assert saves == [False]  # the result only, nothing "tampered" on the way
    real(None, None, None, 0, 0.0, tampered=True)  # a verdict kept meanwhile
    GATE.reset()
    call_command("ledger_status", "--from-scratch", stdout=StringIO())
    kept = ledger_status._load()
    assert kept is not None
    assert kept["tampered"] is False
    assert kept["verified"] is not None
    assert kept["verified"].seq == 4
    assert probe() == "erasure_ledger_gate=ok\n"


def test_a_probe_reading_a_body_that_is_not_json_keeps_no_verdict(
    ledger_dir, admin, user_a, caplog
):
    """Info I-5: a body that isn't JSON (a proxy's error page, a torn write) answers
    "tampered" for that probe, without the sticky verdict a chain mismatch keeps."""
    import logging

    for _ in range(2):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"  # 2 entries kept as verified
    erase(admin, user_a)
    path = ledger_dir / f"{3:012d}.json"
    good = path.read_bytes()
    path.write_bytes(b"<html><body>502 Bad Gateway</body></html>")
    for fresh_start in (False, True):  # the gate's check from the kept state, then the start
        if fresh_start:
            _state_file().unlink()
        caplog.clear()
        with (
            caplog.at_level(logging.WARNING, logger="arkray.core.ledger_gate"),
            pytest.raises(CommandError, match="tampered"),
        ):
            probe()
        assert any(r.getMessage() == "erasure_ledger_malformed" for r in caplog.records)
        kept = ledger_status._load()
        assert kept is None or kept["tampered"] is False
    path.write_bytes(good)
    assert probe() == "erasure_ledger_gate=ok\n"


def test_a_state_that_cannot_be_kept_never_trusts_a_checkpoint(
    ledger_dir, admin, user_a, monkeypatch, caplog
):
    """Info (review round 3): with the temporary directory unwritable, every probe started
    from the newest checkpoint, the pass from scratch never ran, and an altered entry below
    it was never reported."""
    import logging

    monkeypatch.setattr(ledger, "CHECKPOINT_EVERY", 2)
    for _ in range(4):
        erase(admin, user_a)
    ledger.verified_state()  # checkpoints at 2 and 4
    _alter(ledger_dir, 1)

    def unwritable(*args, **kwargs):
        raise PermissionError(13, "read-only file system")

    monkeypatch.setattr(ledger_status.os, "replace", unwritable)
    with caplog.at_level(logging.WARNING, logger="arkray.core.ledger_gate"):
        for _ in range(3):
            with pytest.raises(CommandError, match="tampered"):
                probe()
    assert any(r.getMessage() == "erasure_ledger_status_unsaved" for r in caplog.records)


def test_a_probe_never_clears_a_tampered_verdict_kept_meanwhile(ledger_dir, admin, user_a):
    """Info I-3: a probe saved its result without reading the state again, so one that began
    before another probe's tampered verdict could clear it."""
    for _ in range(3):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    kept = ledger_status._load()
    assert kept is not None
    ledger_status._save(None, None, None, 0, 0.0, tampered=True)  # another probe, meanwhile
    ledger_status._save(kept["verified"], None, None, 0, ledger_status._now())
    again = ledger_status._load()
    assert again is not None
    assert again["tampered"] is True


# --- review round 4 ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [b"<html><body>502 Bad Gateway</body></html>", b"1", b"[1]", b"null"],
    ids=["html", "int", "list", "null"],
)
def test_an_old_entry_that_is_no_entry_any_more_is_kept_as_tampering(
    ledger_dir, admin, user_a, monkeypatch, caplog, body
):
    """LEDGER4-1 (a volume ledger): an entry verified before, replaced by a body that isn't an
    entry, found by the pass over the whole chain: tampering, kept, and the security event at
    every probe."""
    import logging

    monkeypatch.setattr(ledger_status, "FULL_CHECK_EVERY_S", 0)  # the pass at every probe
    for _ in range(4):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    (ledger_dir / f"{2:012d}.json").write_bytes(body)
    with caplog.at_level(logging.WARNING, logger="arkray.core.ledger_gate"):
        for _ in range(2):
            with pytest.raises(CommandError, match="tampered"):
                probe()
    events = [r for r in caplog.records if r.getMessage() == "erasure_ledger_tampered"]
    assert len(events) == 2, [r.getMessage() for r in caplog.records]
    assert all(r.levelno == logging.ERROR for r in events)
    kept = ledger_status._load()
    assert kept is not None
    assert kept["tampered"] is True


def test_a_kept_verdict_is_logged_whatever_the_read_that_fails(
    ledger_dir, admin, user_a, monkeypatch, caplog
):
    """LEDGER4-1: after a genuine mismatch, a later read meeting a body that isn't JSON
    downgraded the event to a WARNING."""
    import logging

    monkeypatch.setattr(ledger_status, "FULL_CHECK_EVERY_S", 0)  # the pass at every probe
    for _ in range(4):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    _alter(ledger_dir, 2)  # below the head: found by the pass
    with pytest.raises(CommandError, match="tampered"):
        probe()
    (ledger_dir / f"{1:012d}.json").write_bytes(b"<html></html>")
    caplog.clear()
    with (
        caplog.at_level(logging.WARNING, logger="arkray.core.ledger_gate"),
        pytest.raises(CommandError, match="tampered"),
    ):
        probe()
    found = [(r.getMessage(), r.levelno) for r in caplog.records]
    assert ("erasure_ledger_tampered", logging.ERROR) in found, found


def test_an_operator_s_pass_keeps_an_unreadable_old_entry_as_tampering(
    ledger_dir, admin, user_a, caplog
):
    """LEDGER4-1: `--from-scratch` meeting an entry this container verified before, replaced
    by a body that isn't JSON, answered "tampered" with a WARNING and kept nothing."""
    import logging

    for _ in range(4):
        erase(admin, user_a)
    assert probe() == "erasure_ledger_gate=ok\n"
    (ledger_dir / f"{2:012d}.json").write_bytes(b"<html></html>")
    GATE.reset()
    with (
        caplog.at_level(logging.WARNING, logger="arkray.core.ledger_gate"),
        pytest.raises(CommandError, match="tampered"),
    ):
        call_command("ledger_status", "--from-scratch", stdout=StringIO())
    found = [(r.getMessage(), r.levelno) for r in caplog.records]
    assert ("erasure_ledger_tampered", logging.ERROR) in found, found
    kept = ledger_status._load()
    assert kept is not None
    assert kept["tampered"] is True
