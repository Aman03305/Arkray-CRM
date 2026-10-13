"""Checking the erasure ledger costs the same at any size (security review M3; core.ledger,
core.ledger_gate, core.health; docs/reliability.md, docs/runbooks.md#erasure-ledger-behind).

Every gate check (each process, every ERASURE_LEDGER_CHECK_INTERVAL_S, the readiness probe
included) and every append read the ledger's head, and the S3 store found it by listing the
whole prefix (one LIST per 1,000 entries; the file store, a directory listing). At around
100,000 entries each check took seconds and readiness failed. Now the head costs one small
LIST and one GET from where the process last saw it (one existence check for a file ledger),
and O(log n) small LISTs in a process that never saw it; entries are read only after the last
verified one. A slow ledger never holds a request or a readiness probe longer than
ledger_gate.CHECK_WAIT_S: the gate answers from its last good state meanwhile (open for
ERASURE_LEDGER_MAX_STALE_S after a verified comparison, closed in a process that never had
one). Truncation, rollback and tampering are still caught.
"""

from __future__ import annotations

import bisect
import io
import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from django.db import transaction
from django.test import override_settings

from arkray.core import ledger, ledger_gate
from arkray.core.ledger_gate import GATE
from arkray.core.models import LedgerState
from tests.factories import LeadFactory
from tests.helpers import signed_in

KEY = "ledger-test-key-" + "x" * 32
BUCKET, PREFIX = "arkray-ledger", "prod"
URL = f"s3://{BUCKET}/{PREFIX}"
HUGE = 200_000
LAST_CHAR = chr(0x10FFFF)


class FakeS3:
    """An S3 bucket in memory, as list_objects_v2 sees one: keys in lexicographic order,
    `Prefix`, `StartAfter`, `MaxKeys` (1,000 at most a page) and continuation tokens. Counts
    every call and keeps each LIST's arguments."""

    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}
        self._keys: list[str] = []
        self.calls: Counter[str] = Counter()
        self.lists: list[dict[str, Any]] = []

    def put(self, key: str, body: bytes) -> None:
        if key not in self.bodies:
            bisect.insort(self._keys, key)
        self.bodies[key] = body

    def remove(self, key: str) -> None:
        del self.bodies[key]
        self._keys.remove(key)

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["list_objects_v2"] += 1
        self.lists.append(kwargs)
        prefix = kwargs.get("Prefix", "")
        limit = min(int(kwargs.get("MaxKeys", 1000)), 1000)
        after = kwargs.get("ContinuationToken") or kwargs.get("StartAfter") or ""
        low = bisect.bisect_left(self._keys, prefix)
        high = bisect.bisect_left(self._keys, prefix + LAST_CHAR)
        start = max(low, bisect.bisect_right(self._keys, after)) if after else low
        page = self._keys[start : min(start + limit, high)]
        truncated = start + limit < high
        answer: dict[str, Any] = {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": truncated,
            "KeyCount": len(page),
        }
        if truncated:
            answer["NextContinuationToken"] = page[-1]
        return answer

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 — boto3's names
        self.calls["get_object"] += 1
        if Key not in self.bodies:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.bodies[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs: Any) -> dict[str, Any]:  # noqa: N803
        self.calls["put_object"] += 1
        if kwargs.get("IfNoneMatch") == "*" and Key in self.bodies:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.put(Key, Body)
        return {}

    def reset_counts(self) -> None:
        self.calls.clear()
        self.lists.clear()


def key(seq: int) -> str:
    return f"{PREFIX}/{seq:012d}.json"


class Chain:
    """Entries 1 to `size` in a FakeS3. With `signed_from` above 1, the entries before it are
    placeholders naming only their number, which any verification reading them refuses (so
    a check that reads one fails): building 200,000 signed entries would take minutes, and
    reading them is what must never happen. The entries from `signed_from` on are signed and
    chained; `states` holds the verified state after each multiple of 1,000 of them (what a
    process that verified that far keeps)."""

    def __init__(self, size: int, *, signed_from: int = 1) -> None:
        self.s3 = FakeS3()
        self.entries: dict[int, ledger.Entry] = {}
        self.states: dict[int, ledger.Checked] = {}
        for seq in range(1, min(signed_from, size + 1)):
            self.s3.put(key(seq), b'{"seq": %d}' % seq)
        previous = "f" * 64 if signed_from > 1 else ""
        state = ledger.Checked(
            seq=signed_from - 1,
            mac=previous,
            signers=frozenset({ledger.key_id(KEY)}) if signed_from > 1 else frozenset(),
        )
        self.states[signed_from - 1] = state
        pending: list[dict[str, Any]] = []
        for seq in range(signed_from, size + 1):
            entry = self.append(seq, prev=previous, at="2026-10-12T00:00:00+00:00")
            previous = entry.mac
            pending.append(json.loads(entry.as_json()))
            if seq % 1000 == 0 or seq == size:
                _, state = ledger.check(pending, state)
                self.states[seq] = state
                pending = []

    def copy(self) -> Chain:
        twin = Chain(0)
        twin.s3.bodies, twin.s3._keys = dict(self.s3.bodies), list(self.s3._keys)
        twin.entries, twin.states = dict(self.entries), dict(self.states)
        return twin

    def state_at(self, seq: int) -> ledger.Checked:
        base = max(s for s in self.states if s <= seq)
        rows = [json.loads(self.entries[n].as_json()) for n in range(base + 1, seq + 1)]
        return ledger.check(rows, self.states[base])[1] if rows else self.states[base]

    def append(
        self, seq: int, *, prev: str | None = None, at: str = "2026-10-12T01:00:00+00:00"
    ) -> ledger.Entry:
        draft = ledger.Entry(
            seq=seq,
            kind="lead_erased",
            subject=f"00000000-0000-0000-0000-{seq:012d}",
            at=at,
            prev=self.entries[seq - 1].mac if prev is None else prev,
            mac="",
        )
        entry = ledger.Entry(**{**draft.content(), "mac": ledger._mac(draft.content(), KEY)})
        self.entries[seq] = entry
        self.s3.put(key(seq), entry.as_json())
        return entry

    def clutter(self) -> Chain:
        """200 checkpoint objects where the store keeps them, and 150 other objects under
        the prefix that sort after every entry (review round 2, LEDGER2-2)."""
        store = ledger.S3Store(URL)
        for n in range(200):
            self.s3.put(store._checkpoint_key(f"{n:012d}.clutter.json"), b"{}")
        for n in range(150):
            self.s3.put(f"{PREFIX}/zz-notes-{n:03d}.txt", b"")
        return self

    def drop(self, *seqs: int) -> None:
        """Entries removed from the bucket (and forgotten here)."""
        for seq in seqs:
            self.s3.remove(key(seq))
            self.entries.pop(seq, None)


_chains: dict[tuple[int, int], Chain] = {}


def chain(size: int, signed_from: int = 1) -> Chain:
    if (size, signed_from) not in _chains:
        with override_settings(ERASURE_LEDGER_KEY=KEY, ERASURE_LEDGER_VERIFY_KEYS=[]):
            _chains[size, signed_from] = Chain(size, signed_from=signed_from).clutter()
    return _chains[size, signed_from]


def huge() -> Chain:
    """200,000 entries, the last 1,000 signed (a copy of its own)."""
    return chain(HUGE, HUGE - 999).copy()


@pytest.fixture
def s3_ledger(settings, monkeypatch):
    """Points ERASURE_LEDGER_URL at a FakeS3 (set `.client` before use)."""
    settings.ERASURE_LEDGER_URL = URL
    settings.ERASURE_LEDGER_KEY = KEY
    settings.ERASURE_LEDGER_VERIFY_KEYS = []
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 0
    holder: dict[str, FakeS3] = {}
    monkeypatch.setattr(ledger.S3Store, "client", property(lambda self: holder["client"]))
    GATE.reset()
    yield holder
    _settle()
    GATE.reset()


def _settle() -> None:
    """Wait for a read of the ledger this process left running (a slow one a test started):
    it would otherwise finish during the next test."""
    job = GATE._job
    if job is not None:
        job.done.wait(15)


def fresh(size: int) -> Chain:
    """A fully signed chain of its own (the tests that change it never share it)."""
    return chain(size).copy()


def sized(size: int) -> Chain:
    """A chain of `size` entries to read, never change (HUGE: mostly placeholders)."""
    return chain(HUGE, HUGE - 999) if size == HUGE else chain(size)


# --- the cost of the head ------------------------------------------------------------------------
@pytest.mark.parametrize("size", [1_000, HUGE], ids=["1k", "200k"])
def test_a_known_head_costs_one_small_list_and_one_get(s3_ledger, size):
    found = sized(size)
    s3_ledger["client"] = found.s3
    found.s3.reset_counts()
    head = ledger.head(size)
    assert head is not None
    assert (head.seq, head.mac) == (size, found.entries[size].mac)
    assert found.s3.calls == {"list_objects_v2": 1, "get_object": 1}
    assert all(int(call.get("MaxKeys", 1000)) <= 100 for call in found.s3.lists)


@pytest.mark.parametrize("size", [1_000, HUGE], ids=["1k", "200k"])
def test_a_head_found_from_scratch_costs_a_logarithmic_number_of_lists(s3_ledger, size):
    found = sized(size)
    s3_ledger["client"] = found.s3
    found.s3.reset_counts()
    head = ledger.head()
    assert head is not None
    assert head.seq == size
    assert found.s3.calls["get_object"] == 1
    assert found.s3.calls["list_objects_v2"] <= 2 * math.ceil(math.log2(size)) + 2
    assert all(int(call.get("MaxKeys", 1000)) <= 100 for call in found.s3.lists)


def test_a_head_moved_since_costs_a_few_lists(s3_ledger):
    found = fresh(5_000)
    s3_ledger["client"] = found.s3
    found.s3.reset_counts()
    head = ledger.head(1_000)  # 4,000 entries added since this process looked
    assert head is not None
    assert head.seq == 5_000
    assert found.s3.calls["list_objects_v2"] <= 2 * math.ceil(math.log2(4_000)) + 2


@pytest.mark.django_db
def test_the_gate_reads_only_what_was_added_to_a_huge_ledger(s3_ledger):
    found = huge()
    s3_ledger["client"] = found.s3
    GATE.resume(found.state_at(HUGE))  # a primed process: verified up to the head
    for seq in range(HUGE + 1, HUGE + 4):  # three erasures since
        found.append(seq)
    ledger.mark_applied(found.entries[HUGE + 3])
    found.s3.reset_counts()
    assert GATE.status() == "ok"
    assert found.s3.calls["list_objects_v2"] <= 3
    # The head, the entry last verified (still there, unchanged), and the three new ones.
    assert found.s3.calls["get_object"] <= 5
    found.s3.reset_counts()
    assert GATE.status() == "ok"  # nothing new: one LIST, one GET
    assert found.s3.calls == {"list_objects_v2": 1, "get_object": 1}


@pytest.mark.django_db
def test_an_append_to_a_huge_ledger_costs_the_same(s3_ledger):
    found = huge()
    s3_ledger["client"] = found.s3
    ledger.mark_applied(found.entries[HUGE])
    found.s3.reset_counts()
    with transaction.atomic():
        entry = ledger.append("lead_erased", "22222222-0000-0000-0000-000000000001")
    assert entry is not None
    assert (entry.seq, entry.prev) == (HUGE + 1, found.entries[HUGE].mac)
    assert found.s3.calls == {"list_objects_v2": 1, "get_object": 1, "put_object": 1}
    assert ledger.applied() == (HUGE + 1, entry.mac)


# --- still caught ----------------------------------------------------------------------------
@pytest.fixture
def watched(s3_ledger, db):
    """A 2,000-entry S3 ledger this process has verified and the database has applied."""
    found = fresh(2_000)
    s3_ledger["client"] = found.s3
    GATE.resume(found.state_at(2_000))
    ledger.mark_applied(found.entries[2_000])
    assert GATE.status() == "ok"
    return found


def test_a_truncated_ledger_closes_the_gate(watched):
    watched.drop(2_000, 1_999, 1_998)
    assert GATE.status() == "ahead"


def test_a_ledger_rolled_back_and_regrown_closes_the_gate(watched):
    watched.drop(*range(1_990, 2_001))
    for seq in range(1_990, 2_003):  # other entries in their place, properly chained
        watched.append(seq)
    assert GATE.status() in {"tampered", "diverged"}


def test_an_altered_head_closes_the_gate(watched):
    data = json.loads(watched.s3.bodies[key(2_000)])
    data["subject"] = "00000000-0000-0000-0000-000000000000"
    watched.s3.put(key(2_000), json.dumps(data).encode())
    assert GATE.status() == "tampered"


def test_an_entry_under_another_entrys_name_closes_the_gate(watched):
    watched.s3.put(key(2_000), watched.s3.bodies[key(1_000)])  # validly signed, wrong place
    assert GATE.status() == "tampered"


def test_a_new_entry_that_breaks_the_chain_closes_the_gate(watched):
    watched.append(2_001, prev="0" * 64)
    ledger.mark_applied(watched.entries[2_001])
    assert GATE.status() == "tampered"


def test_a_gap_in_the_new_entries_closes_the_gate(watched):
    for seq in (2_001, 2_002, 2_003):
        watched.append(seq)
    watched.drop(2_002)
    ledger.mark_applied(watched.entries[2_003])
    assert GATE.status() == "tampered"


def test_the_entry_last_verified_removed_closes_the_gate(watched):
    """Entries are read from the last verified one on, not from the start: that one must
    still be there, unchanged, or a deletion just before the new entries would pass."""
    for seq in (2_001, 2_002):
        watched.append(seq)
    watched.drop(2_000)
    ledger.mark_applied(watched.entries[2_002])
    assert GATE.status() == "tampered"


def test_a_head_stored_under_another_entrys_name_is_refused(s3_ledger):
    found = fresh(2_000)
    s3_ledger["client"] = found.s3
    found.s3.put(key(2_000), found.s3.bodies[key(1_999)])
    with pytest.raises(ledger.LedgerTampered, match="isn't the entry its name says"):
        ledger.head(2_000)
    with pytest.raises(ledger.LedgerTampered, match="isn't the entry its name says"):
        ledger.head()


def test_an_old_entry_removed_is_caught_by_a_process_that_checks_the_whole_chain(watched):
    watched.drop(10)
    GATE.reset()  # a new process: verifies everything
    assert GATE.status() == "tampered"
    with pytest.raises(ledger.LedgerTampered):
        ledger.verify()


# --- a file ledger ---------------------------------------------------------------------------
@pytest.fixture
def ledger_dir(settings, tmp_path):
    directory = tmp_path / "ledger"
    directory.mkdir()
    settings.ERASURE_LEDGER_URL = directory.as_uri()
    settings.ERASURE_LEDGER_KEY = KEY
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 0
    GATE.reset()
    yield directory
    _settle()
    GATE.reset()


def erase(admin, owner):
    from arkray.privacy import services

    services.erase(LeadFactory(owner=owner).pk, operator_id=admin.pk)


@pytest.mark.django_db
def test_a_file_ledgers_head_lists_no_directory(ledger_dir, admin, user_a, monkeypatch):
    for _ in range(3):
        erase(admin, user_a)
    assert GATE.status() == "ok"
    listed: list[str] = []
    real_glob, real_scandir = Path.glob, ledger.os.scandir

    def glob(self, pattern):
        listed.append(str(self))
        return real_glob(self, pattern)

    def scandir(path="."):
        listed.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(Path, "glob", glob)
    monkeypatch.setattr(ledger.os, "scandir", scandir)
    assert ledger.head(3).seq == 3
    assert GATE.status() == "ok"  # nothing new: no listing at all
    assert listed == []
    assert ledger.head().seq == 3  # from scratch: existence checks, still no listing
    assert listed == []


@pytest.mark.django_db
def test_a_truncated_file_ledger_closes_the_gate(ledger_dir, admin, user_a):
    for _ in range(3):
        erase(admin, user_a)
    assert GATE.status() == "ok"
    (ledger_dir / "000000000003.json").unlink()
    assert GATE.status() == "ahead"


# --- bounded readiness -------------------------------------------------------------------------
def _slow_head(monkeypatch, seconds: float) -> None:
    found = ledger.store()
    assert found is not None
    store = type(found)
    real = store.last

    def slow(self, *args, **kwargs):
        time.sleep(seconds)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(store, "last", slow)


@pytest.mark.django_db
def test_a_slow_ledger_never_holds_readiness_past_its_bound(ledger_dir, admin, user_a, monkeypatch):
    erase(admin, user_a)
    assert GATE.status() == "ok"  # a verified comparison: a brief slowness keeps it open
    _slow_head(monkeypatch, 6.0)
    client = signed_in(user_a)
    started = time.monotonic()
    ready = client.get("/health/ready")
    took = time.monotonic() - started
    assert ready.status_code == 200, ready.content
    assert took < ledger_gate.CHECK_WAIT_S + 2.0, took
    started = time.monotonic()
    assert client.get("/api/v1/auth/me").status_code == 200
    assert time.monotonic() - started < ledger_gate.CHECK_WAIT_S + 2.0


@pytest.mark.django_db
def test_a_process_that_never_verified_stays_closed_while_the_ledger_is_slow(
    ledger_dir, admin, user_a, monkeypatch
):
    erase(admin, user_a)
    GATE.reset()  # a new process
    _slow_head(monkeypatch, 6.0)
    client = signed_in(user_a)
    started = time.monotonic()
    ready = client.get("/health/ready")
    took = time.monotonic() - started
    assert ready.status_code == 503
    assert took < ledger_gate.CHECK_WAIT_S + 2.0, took
    assert not ledger_gate.is_open()


@pytest.mark.django_db
def test_the_gate_opens_once_a_slow_first_read_finishes(ledger_dir, admin, user_a, monkeypatch):
    erase(admin, user_a)
    GATE.reset()
    _slow_head(monkeypatch, ledger_gate.CHECK_WAIT_S + 1.0)
    assert not ledger_gate.is_open()  # still reading
    time.sleep(1.5)
    monkeypatch.undo()
    deadline = time.monotonic() + 10
    while not ledger_gate.is_open():
        assert time.monotonic() < deadline, "the gate never opened"
        time.sleep(0.2)
    assert GATE.status() == "ok"


@pytest.mark.django_db
def test_a_long_first_read_reports_what_it_found(ledger_dir, admin, user_a, settings, monkeypatch):
    """A first read longer than the check interval that finds the chain tampered with is
    reported as such, not started again (and reported "unavailable") at every check."""
    erase(admin, user_a)
    erase(admin, user_a)
    first = ledger_dir / "000000000001.json"
    data = json.loads(first.read_text())
    data["subject"] = "00000000-0000-0000-0000-000000000000"
    first.write_text(json.dumps(data))
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 1
    GATE.reset()
    _slow_head(monkeypatch, ledger_gate.CHECK_WAIT_S + 0.5)
    with ledger_gate.bounded():
        assert GATE.status(now=1000.0) == "unavailable"  # still reading
    _settle()
    with ledger_gate.bounded():
        assert GATE.status(now=1002.0) == "tampered"


@pytest.mark.django_db
def test_a_database_restored_behind_a_huge_ledger_is_closed(s3_ledger):
    found = huge()
    s3_ledger["client"] = found.s3
    restored = HUGE - 500  # the backup's position
    LedgerState.objects.update_or_create(
        pk=1, defaults={"applied_seq": restored, "applied_mac": found.entries[restored].mac}
    )
    GATE.resume(found.state_at(HUGE))
    found.s3.reset_counts()
    assert GATE.status() == "behind"
    assert found.s3.calls["list_objects_v2"] <= 2


# --- review round 1 ------------------------------------------------------------------------------
class SlowS3:
    """A FakeS3 whose every call takes `delay` seconds: a store slower than the gate waits."""

    def __init__(self, s3: FakeS3, delay: float) -> None:
        self.s3, self.delay = s3, delay

    def __getattr__(self, name: str) -> Any:
        real = getattr(self.s3, name)
        if name not in {"list_objects_v2", "get_object", "put_object"}:
            return real

        def slow(*args: Any, **kwargs: Any) -> Any:
            time.sleep(self.delay)
            return real(*args, **kwargs)

        return slow


SLOW_S = 1.5  # every S3 call (LEDGER-1's reproduction)


@pytest.fixture
def slow_store(s3_ledger, db, settings):
    """2,000 entries this process verified and the database applied, checked at the
    production interval; from then on every S3 call takes SLOW_S."""
    found = fresh(2_000)
    s3_ledger["client"] = found.s3
    GATE.resume(found.state_at(2_000))
    ledger.mark_applied(found.entries[2_000])
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 60
    assert GATE.status() == "ok"
    s3_ledger["client"] = SlowS3(found.s3, SLOW_S)
    return found


def answers(seconds: float, *, settle_after_closed: float = 3.0) -> list[tuple[float, str]]:
    """The gate as requests see it (bounded), every 0.5 s for up to `seconds`, starting one
    interval after the last check: (seconds since the start, status) pairs. Stops
    `settle_after_closed` s after the first closed answer."""
    found: list[tuple[float, str]] = []
    start = time.monotonic()
    closed_at = None
    while time.monotonic() - start < seconds:
        with ledger_gate.bounded():
            status = GATE.status(now=time.monotonic() + 61)
        at = time.monotonic() - start
        found.append((at, status))
        if status not in ledger_gate.OPEN and closed_at is None:
            closed_at = at
        if closed_at is not None and at - closed_at >= settle_after_closed:
            break
        time.sleep(0.5)
    return found


def _closes_within(seen: list[tuple[float, str]], bound: float, status: str) -> None:
    closed = [at for at, answer in seen if answer not in ledger_gate.OPEN]
    assert closed, seen
    first = closed[0]
    assert first <= bound, seen
    after = [answer for at, answer in seen if at >= first]
    assert set(after) == {status}, seen  # and stays closed


def _checked_every(seconds: int, found: Chain, s3_ledger, settings) -> None:
    """The gate's last check made at a short interval (real time, as in production, but
    `seconds` apart), on the quick store; then every S3 call takes SLOW_S again."""
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = seconds
    s3_ledger["client"] = found.s3
    GATE.reset()
    GATE.resume(found.state_at(2_000))
    assert GATE.status() == "ok"
    s3_ledger["client"] = SlowS3(found.s3, SLOW_S)


def test_a_consistently_slow_store_still_closes_a_database_behind_it(
    slow_store, s3_ledger, settings
):
    """LEDGER-1: every read slower than CHECK_WAIT_S answered "unavailable", which the
    stale window turned into "ok": open for up to an hour on un-replayed data. Real time,
    over several check intervals (review round 3: a constant offset never checked again)."""
    _checked_every(3, slow_store, s3_ledger, settings)
    LedgerState.objects.filter(pk=1).update(
        applied_seq=1_990, applied_mac=slow_store.entries[1_990].mac
    )
    bound = 3 + 4 * SLOW_S + 2
    _closes_within(real_answers(bound + 9), bound, "behind")
    _settle()


def test_a_consistently_slow_store_still_closes_a_rolled_back_ledger(
    slow_store, s3_ledger, settings
):
    """LEDGER3-1: a decided "ahead" reopened at each later check while its read was still
    running (the stale window): ok, ahead, ok, ahead..."""
    _checked_every(3, slow_store, s3_ledger, settings)
    slow_store.drop(*range(1_991, 2_001))  # the ledger rolled back by 10 entries
    bound = 3 + 6 * SLOW_S + 2
    _closes_within(real_answers(bound + 12), bound, "ahead")
    _settle()


def test_a_restore_is_closed_at_once_without_reading_a_slow_store(slow_store):
    """Entries this process verified are in the ledger for good: a database behind them is
    behind, with no read of the ledger."""
    LedgerState.objects.filter(pk=1).update(
        applied_seq=100, applied_mac=slow_store.entries[100].mac
    )
    slow_store.s3.reset_counts()
    started = time.monotonic()
    with ledger_gate.bounded():
        assert GATE.status(now=time.monotonic() + 61) == "behind"
    assert time.monotonic() - started < 1.0
    assert slow_store.s3.calls == {}


@pytest.mark.django_db
def test_the_behind_path_waits_for_one_read_only(ledger_dir, admin, user_a, monkeypatch):
    """LEDGER-4: confirming "behind" started a second read, so the check took up to twice
    CHECK_WAIT_S. It now confirms with the database's position alone."""
    erase(admin, user_a)
    erase(admin, user_a)
    assert GATE.status() == "ok"
    store = ledger.store()
    assert store is not None
    last = ledger.head(2)
    assert last is not None
    draft = ledger.Entry(
        seq=3, kind="lead_erased", subject=str(user_a.pk), at=last.at, prev=last.mac, mac=""
    )
    entry = ledger.Entry(**{**draft.content(), "mac": ledger._mac(draft.content())})
    assert store.create(3, entry.as_json())  # an erasure whose commit failed after its entry
    _slow_head(monkeypatch, 1.5)
    started = time.monotonic()
    status = ledger_gate.bounded_status()
    took = time.monotonic() - started
    assert status == "behind"
    assert took < ledger_gate.CHECK_WAIT_S + 0.4, took


def test_a_restored_database_is_closed_while_a_huge_ledger_is_slow(s3_ledger, db, settings):
    """Test gap of the first round: the restore case only ran unbounded."""
    found = huge()
    restored = HUGE - 500
    LedgerState.objects.update_or_create(
        pk=1, defaults={"applied_seq": restored, "applied_mac": found.entries[restored].mac}
    )
    GATE.resume(found.state_at(HUGE))
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 60
    s3_ledger["client"] = SlowS3(found.s3, SLOW_S)
    bound = ledger_gate.PENDING_RECHECK_S + 4 * SLOW_S + 2
    _closes_within(answers(bound + 4), bound, "behind")
    _settle()


# --- start-up: signed checkpoints, bounded memory --------------------------------------------
def _checkpoints(found: Chain) -> list[str]:
    """The checkpoints a verification wrote (not the module's `clutter`)."""
    where = ledger.S3Store(URL)._checkpoint_key("")
    return sorted(k for k in found.s3.bodies if k.startswith(where) and "clutter" not in k)


def test_a_verification_leaves_signed_checkpoints_without_personal_data(s3_ledger):
    found = fresh(3_000)
    s3_ledger["client"] = found.s3
    assert ledger.verified_state().seq == 3_000
    kept = _checkpoints(found)
    # Never under the entries' prefix: no listing of it (this release's or an earlier one's,
    # or any tool's) pages through them (review round 2, LEDGER2-2).
    assert kept
    assert not any(k.startswith(f"{PREFIX}/") for k in kept)
    assert [k.split("/")[-1].split(".")[0] for k in kept] == [
        "000000001000",
        "000000002000",
        "000000003000",
    ]
    body = json.loads(found.s3.bodies[kept[-1]])
    assert body["seq"] == 3_000
    assert body["mac"] == found.entries[3_000].mac
    assert KEY not in json.dumps(body)
    assert not any(e.subject in json.dumps(body) for e in found.entries.values())


@pytest.mark.django_db
def test_a_process_start_reads_from_the_newest_checkpoint(s3_ledger):
    """LEDGER-2 (b): prime() read the whole chain (one GET per entry) at every start."""
    found = fresh(5_000)
    s3_ledger["client"] = found.s3
    ledger.verified_state()  # a process verified it once: checkpoints up to 5,000
    for seq in range(5_001, 5_011):
        found.append(seq)
    ledger.mark_applied(found.entries[5_010])
    found.s3.reset_counts()
    GATE.reset()
    ledger_gate.prime()
    state = GATE.verified_state()
    assert state is not None
    assert state.seq == 5_010
    assert found.s3.calls["get_object"] <= 20, found.s3.calls
    assert GATE.status() == "ok"


@pytest.mark.django_db
def test_a_checkpoint_never_hides_a_restore(s3_ledger):
    found = fresh(5_000)
    s3_ledger["client"] = found.s3
    ledger.verified_state()
    LedgerState.objects.update_or_create(
        pk=1, defaults={"applied_seq": 4_000, "applied_mac": found.entries[4_000].mac}
    )
    GATE.reset()
    ledger_gate.prime()
    assert GATE.status() == "behind"


def _forge_checkpoint(found: Chain, seq: int, changes: dict[str, Any], resign: bool) -> None:
    """A checkpoint changed (and, with `resign`, signed again with the ledger key)."""
    name = next(k for k in _checkpoints(found) if f"/{seq:012d}." in k)
    body = json.loads(found.s3.bodies[name])
    body.update(changes)
    if resign:
        content = {k: v for k, v in body.items() if k != "sig"}
        with override_settings(ERASURE_LEDGER_KEY=KEY):
            body["sig"] = ledger._checkpoint_sig(content)
    found.s3.put(name, json.dumps(body).encode())


@pytest.mark.parametrize(
    ("changes", "resign"),
    [({"digest": "0" * 64}, False), ({"retired": ["k"]}, False), ({"mac": "1" * 64}, True)],
    ids=["altered", "altered-keys", "signed-but-not-the-entry"],
)
def test_a_checkpoint_that_does_not_verify_is_ignored(s3_ledger, changes, resign):
    found = fresh(5_000)
    s3_ledger["client"] = found.s3
    ledger.verified_state()
    for seq in (5_000, 4_000, 3_000):
        _forge_checkpoint(found, seq, changes, resign)
    found.s3.reset_counts()
    GATE.reset()
    ledger_gate.prime()
    state = GATE.verified_state()
    assert state is not None
    assert state.seq == 5_000
    assert found.s3.calls["get_object"] >= 5_000  # the whole chain, as before


def test_a_checkpoint_signed_with_another_key_is_ignored(s3_ledger, settings):
    found = fresh(2_000)
    s3_ledger["client"] = found.s3
    ledger.verified_state()
    settings.ERASURE_LEDGER_KEY = "another-ledger-key-" + "y" * 32
    settings.ERASURE_LEDGER_VERIFY_KEYS = [KEY]  # entries still verify, checkpoints don't
    assert ledger.checkpoint() is None


@pytest.mark.django_db
def test_verifying_a_whole_chain_holds_one_chunk_at_a_time(s3_ledger):
    """LEDGER-3: every entry was fetched at once and kept (495 MiB at 200,000 entries
    against 190 MiB before): a start-up could exhaust a small worker's memory."""
    import tracemalloc

    found = fresh(10_000)
    s3_ledger["client"] = found.s3
    GATE.reset()
    tracemalloc.start()
    try:
        before, _ = tracemalloc.get_traced_memory()
        ledger_gate.prime()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    state = GATE.verified_state()
    assert state is not None
    assert state.seq == 10_000
    assert peak - before < 6 * 1024 * 1024, peak - before


@pytest.mark.django_db
def test_a_first_health_probe_without_a_state_file_is_bounded(s3_ledger, tmp_path, monkeypatch):
    """LEDGER-2 (b): a container's first `ledger_status` verified the whole chain (33 s at
    10,000 entries, past the 30 s health-check timeout); a killed probe kept nothing, so a
    restarted worker never turned healthy. Each probe now verifies a bounded slice and keeps
    its progress."""
    import tempfile

    from django.core.management import call_command
    from django.core.management.base import CommandError

    from arkray.core.management.commands import ledger_status

    found = fresh(50_000)
    s3_ledger["client"] = found.s3
    ledger.mark_applied(found.entries[50_000])
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    outcomes, gets = [], []
    for _ in range(50_000 // ledger_status.AUDIT_ENTRIES_PER_PROBE + 2):
        GATE.reset()
        found.s3.reset_counts()
        try:
            call_command("ledger_status", stdout=io.StringIO())
            outcomes.append("ok")
        except CommandError as exc:
            outcomes.append(str(exc))
        gets.append(found.s3.calls["get_object"])
        if outcomes[-1] == "ok":
            break
    assert outcomes[-1] == "ok", outcomes
    assert all("verifying" in outcome for outcome in outcomes[:-1]), outcomes
    assert max(gets) <= ledger_status.AUDIT_ENTRIES_PER_PROBE + 20, gets


@pytest.mark.django_db
def test_a_first_health_probe_starts_from_a_checkpoint(s3_ledger, tmp_path, monkeypatch):
    import tempfile

    from django.core.management import call_command

    found = fresh(50_000)
    s3_ledger["client"] = found.s3
    ledger.verified_state()
    ledger.mark_applied(found.entries[50_000])
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    found.s3.reset_counts()
    GATE.reset()
    out = io.StringIO()
    call_command("ledger_status", stdout=out)
    assert out.getvalue() == "erasure_ledger_gate=ok\n"
    assert found.s3.calls["get_object"] <= 20, found.s3.calls


# --- review round 2 ------------------------------------------------------------------------------
def test_checkpoints_and_other_objects_never_cost_the_head_a_page(s3_ledger):
    """LEDGER2-2: checkpoints sat under the listed prefix and sorted after every entry, so
    the head's one LIST paged through them (15 LISTs at 200 checkpoints, 71 at 1,000), in
    every gate check and every append under the append lock. Every chain of this module
    carries 200 checkpoint objects and 150 other objects after the entries (`clutter`)."""
    found = fresh(2_000)
    s3_ledger["client"] = found.s3
    assert sum("checkpoints" in k for k in found.s3.bodies) >= 200
    found.s3.reset_counts()
    head = ledger.head(2_000)
    assert head is not None
    assert head.seq == 2_000
    assert found.s3.calls == {"list_objects_v2": 1, "get_object": 1}


def test_a_bucket_root_ledger_keeps_its_checkpoints_out_of_the_head_s_way(settings, monkeypatch):
    settings.ERASURE_LEDGER_URL = f"s3://{BUCKET}"
    settings.ERASURE_LEDGER_KEY = KEY
    s3 = FakeS3()
    previous = ""
    for seq in range(1, 6):
        draft = ledger.Entry(
            seq=seq, kind="lead_erased", subject=f"s{seq}", at="t", prev=previous, mac=""
        )
        entry = ledger.Entry(**{**draft.content(), "mac": ledger._mac(draft.content(), KEY)})
        previous = entry.mac
        s3.put(f"{seq:012d}.json", entry.as_json())
    store = ledger.S3Store(f"s3://{BUCKET}", client=s3)
    for n in range(300):
        s3.put(store._checkpoint_key(f"{n:012d}.k.json"), b"{}")
    monkeypatch.setattr(ledger.S3Store, "client", property(lambda self: s3))
    head = ledger.head(5)
    assert head is not None
    assert head.seq == 5
    assert s3.calls["list_objects_v2"] == 1
    assert [e.seq for e in ledger.verify()] == [1, 2, 3, 4, 5]


@pytest.mark.django_db
def test_a_recycled_worker_far_behind_opens_within_a_bound(s3_ledger, settings):
    """LEDGER2-3: a worker recycled long after the master primed read every entry added
    since in one go, closed meanwhile (20 s at 5,000 entries on 25 ms GETs). Far behind, it
    now starts from the newest checkpoint."""
    found = fresh(12_000)
    s3_ledger["client"] = found.s3
    ledger.verified_state()  # verified as it grew, by the other processes: checkpoints
    ledger.mark_applied(found.entries[12_000])
    GATE.reset()
    GATE.resume(found.state_at(2_000))  # what the master verified at its start
    settings.ERASURE_LEDGER_CHECK_INTERVAL_S = 60
    s3_ledger["client"] = SlowS3(found.s3, 0.025)
    seen: list[tuple[float, str]] = []
    start = time.monotonic()
    while time.monotonic() - start < 25:
        with ledger_gate.bounded():
            status = GATE.status(now=time.monotonic() + 61)
        seen.append((time.monotonic() - start, status))
        if status == "ok":
            break
        time.sleep(0.5)
    opened = [at for at, status in seen if status == "ok"]
    bound = ledger_gate.CHECK_WAIT_S + ledger_gate.PENDING_RECHECK_S + 6
    assert opened, seen
    assert opened[0] <= bound, seen
    _settle()


@pytest.mark.django_db
def test_gunicorn_refreshes_the_master_before_each_fork(s3_ledger):
    """LEDGER2-3: gunicorn's master calls ledger_gate.refresh before forking each worker.
    gunicorn doesn't import on Windows, so the hook is taken from the configuration and run
    on its own (review round 3: the test only read its source)."""
    import ast

    source = (Path(__file__).resolve().parents[2] / "gunicorn.conf.py").read_text(encoding="utf-8")
    (hook,) = [
        n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "pre_fork"
    ]
    namespace: dict[str, Any] = {}
    # The repository's own configuration file, run as gunicorn would run the hook.
    code = compile(ast.Module(body=[hook], type_ignores=[]), "gunicorn.conf.py", "exec")
    exec(code, namespace)  # noqa: S102
    found = fresh(3_000)
    s3_ledger["client"] = found.s3
    GATE.reset()
    GATE.resume(found.state_at(1_000))
    namespace["pre_fork"](None, None)
    state = GATE.verified_state()
    assert state is not None
    assert state.seq == 3_000


@pytest.mark.django_db
def test_the_master_brings_its_chain_up_to_date_before_each_fork(s3_ledger, monkeypatch):
    """LEDGER2-3: the master's verified chain refreshed (`pre_fork`), bounded."""
    found = fresh(5_000)
    s3_ledger["client"] = found.s3
    GATE.reset()
    GATE.resume(found.state_at(1_000))
    started = time.monotonic()
    ledger_gate.refresh()
    assert time.monotonic() - started < 5
    state = GATE.verified_state()
    assert state is not None
    assert state.seq == 5_000
    # Bounded on a slow store: it stops at its budget, keeping what it verified.
    for seq in range(5_001, 7_001):
        found.append(seq)
    monkeypatch.setattr(ledger_gate, "REFRESH_BUDGET_S", 1.0, raising=False)
    s3_ledger["client"] = SlowS3(found.s3, 0.02)
    started = time.monotonic()
    ledger_gate.refresh()
    assert time.monotonic() - started < 3.0
    state = GATE.verified_state()
    assert state is not None
    assert 5_000 < state.seq <= 7_000


@pytest.mark.django_db
def test_every_health_probe_ends_within_its_deadline_on_a_slow_store(
    s3_ledger, tmp_path, monkeypatch
):
    """LEDGER2-4: a slice was bounded in entries, not time: 32.4 s for 5,000 entries at
    50 ms a GET, past the 30 s timeout, and a killed probe kept nothing."""
    import tempfile

    from django.core.management import call_command
    from django.core.management.base import CommandError

    from arkray.core.management.commands import ledger_status

    found = fresh(5_000)
    ledger.mark_applied(found.entries[5_000])
    s3_ledger["client"] = SlowS3(found.s3, 0.01)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(ledger_status, "PROBE_BUDGET_S", 1.0, raising=False)
    durations, outcomes = [], []
    for _ in range(40):
        GATE.reset()
        started = time.monotonic()
        try:
            call_command("ledger_status", stdout=io.StringIO())
            outcomes.append("ok")
        except CommandError as exc:
            outcomes.append(str(exc))
        durations.append(time.monotonic() - started)
        if outcomes[-1] == "ok":
            break
    assert outcomes[-1] == "ok", outcomes
    assert max(durations) < 1.0 + 2.0, durations


def test_a_gate_that_found_tampering_stays_closed_until_the_chain_verifies(watched, s3_ledger):
    """LEDGER2-1, in a process: once tampered, only a verification of the whole chain from
    scratch reopens it; while that runs on a slow store the answer stays "tampered" (the
    stale window turned it into "ok")."""
    watched.append(2_001, prev="0" * 64)
    ledger.mark_applied(watched.entries[2_001])
    assert GATE.status() == "tampered"
    watched.drop(2_001)
    watched.append(2_001)  # replaced by one that verifies
    ledger.mark_applied(watched.entries[2_001])
    s3_ledger["client"] = SlowS3(watched.s3, 1.0)
    with ledger_gate.bounded():
        assert GATE.status(now=time.monotonic() + 61) == "tampered"
    s3_ledger["client"] = watched.s3  # the store answers quickly again
    _settle()
    with ledger_gate.bounded():
        assert GATE.status(now=time.monotonic() + 122) == "ok"


# --- review round 3 ------------------------------------------------------------------------------
class FailS3:
    """A FakeS3 that answers every call with a server error."""

    def __init__(self, s3: FakeS3) -> None:
        self.s3 = s3

    def __getattr__(self, name: str) -> Any:
        if name not in {"list_objects_v2", "get_object", "put_object"}:
            return getattr(self.s3, name)

        def failing(*args: Any, **kwargs: Any) -> Any:
            raise ClientError({"Error": {"Code": "InternalError"}}, name)

        return failing


def real_answers(seconds: float, *, settle_after_closed: float = 1e9) -> list[tuple[float, str]]:
    """The gate as requests see it (bounded), every 0.5 s, in real time (the status cache
    and the check interval work as in production)."""
    found: list[tuple[float, str]] = []
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        found.append((time.monotonic() - start, ledger_gate.bounded_status()))
        time.sleep(0.5)
    return found


def test_a_decided_verdict_stands_while_the_store_fails(slow_store, s3_ledger):
    """LEDGER3-1: "ahead" decided, then the store failing: the stale window answered ok."""
    s3_ledger["client"] = slow_store.s3  # quick: decide first
    slow_store.drop(*range(1_991, 2_001))
    with ledger_gate.bounded():
        assert GATE.status(now=time.monotonic() + 61) == "ahead"
    s3_ledger["client"] = FailS3(slow_store.s3)
    with ledger_gate.bounded():
        later = [GATE.status(now=time.monotonic() + 61 * k) for k in range(2, 6)]
    assert later == ["ahead"] * 4


@pytest.mark.django_db
def test_an_append_never_moves_the_database_onto_another_chain(s3_ledger):
    """LEDGER3-1: on a ledger rolled back below the database, appends regrew it and the
    eleventh moved the database's position onto the replaced chain (the lost erasures'
    entries accepted silently). An append now refuses unless the database's position is
    the ledger's, entry and MAC."""
    found = fresh(2_000)
    s3_ledger["client"] = found.s3
    ledger.mark_applied(found.entries[2_000])
    before = ledger.applied()
    found.drop(*range(1_991, 2_001))  # rolled back
    for _ in range(11):
        with pytest.raises(ledger.LedgerTampered), transaction.atomic():
            ledger.append("lead_erased", "33333333-0000-0000-0000-000000000001")
    assert ledger.applied() == before
    last = ledger.head()  # nothing written
    assert last is not None
    assert last.seq == 1_990
    for seq in range(1_991, 2_001):  # regrown with other entries, properly chained
        found.append(seq)
    with pytest.raises(ledger.LedgerTampered), transaction.atomic():
        ledger.append("lead_erased", "33333333-0000-0000-0000-000000000002")
    assert ledger.applied() == before


@pytest.mark.django_db
def test_the_master_s_refresh_is_bounded_on_a_slow_store(s3_ledger, monkeypatch):
    """LEDGER3-2: the head, the checkpoint and the first entry's check ran before the budget
    started, and the budget was checked only between 250-entry reads: 15.5 s against 5 s at
    500 ms a call, and the master's arbiter stood still meanwhile."""
    found = fresh(12_000)
    s3_ledger["client"] = found.s3
    ledger.verified_state()  # checkpoints
    GATE.reset()
    GATE.resume(found.state_at(1_000))
    monkeypatch.setattr(ledger_gate, "REFRESH_BUDGET_S", 2.0)
    s3_ledger["client"] = SlowS3(found.s3, 0.5)
    started = time.monotonic()
    ledger_gate.refresh()
    assert time.monotonic() - started < 2.0 + 0.5 + 1.0


@pytest.mark.django_db
def test_the_master_s_refresh_reports_tampering_as_a_security_event(s3_ledger, caplog):
    """Info I-1: the master's refresh meeting a tampered entry logged a WARNING only."""
    import logging

    found = fresh(3_000)
    s3_ledger["client"] = found.s3
    GATE.reset()
    GATE.resume(found.state_at(1_000))
    data = json.loads(found.s3.bodies[key(1_500)])
    data["subject"] = "00000000-0000-0000-0000-000000000000"
    found.s3.put(key(1_500), json.dumps(data).encode())
    with caplog.at_level(logging.WARNING, logger="arkray.core.ledger_gate"):
        ledger_gate.refresh()
    events = [r for r in caplog.records if r.getMessage() == "erasure_ledger_tampered"]
    assert events
    assert events[0].levelno == logging.ERROR
    assert getattr(events[0], "security", False)
    assert GATE.verified_state() is None  # the workers it forks verify from scratch


def test_a_body_that_is_not_json_is_no_lasting_verdict(watched):
    """Info I-5: a body that isn't JSON (a proxy's error page) closed the gate for good, until
    a whole verification from scratch. Beyond what the gate has verified (review round 4,
    LEDGER4-1: below it, it is tampering) it closes it for that check only."""
    watched.append(2_001)
    ledger.mark_applied(watched.entries[2_001])
    good = watched.s3.bodies[key(2_001)]
    watched.s3.put(key(2_001), b"<html><body>502 Bad Gateway</body></html>")
    assert GATE.status() == "tampered"
    assert not GATE.found_tampering()
    watched.s3.put(key(2_001), good)
    watched.s3.reset_counts()
    assert GATE.status() == "ok"
    assert watched.s3.calls["get_object"] <= 3  # not the whole chain again


def test_bounded_calls_use_a_short_timeout_client_of_their_own(settings):
    """LEDGER3-2: a hanging store held the master 20-26 s per call (connect 3 s, read 10 s,
    two attempts), and a client made before the refresh (by `prime`) would keep those."""
    settings.ERASURE_LEDGER_S3_ENDPOINT_URL = "http://127.0.0.1:9"  # never contacted
    settings.ERASURE_LEDGER_S3_REGION = "us-east-1"
    store = ledger.S3Store("s3://arkray-ledger/prod")
    usual = store.client
    with ledger.bounded_calls(5):
        short = store.client
    assert short is not usual
    assert (short.meta.config.connect_timeout, short.meta.config.read_timeout) == (1, 2)
    assert short.meta.config.retries["total_max_attempts"] == 1
    assert (usual.meta.config.connect_timeout, usual.meta.config.read_timeout) == (3, 10)
    assert store.client is usual  # and outside again, the usual one


@pytest.mark.django_db
def test_a_bounded_read_stops_its_parallel_gets_at_the_deadline(s3_ledger):
    """LEDGER3-2: the deadline holds on the threads that fetch a read's entries in parallel
    too, so no read outlasts it by more than one call."""
    found = fresh(200)
    s3_ledger["client"] = SlowS3(found.s3, 0.2)
    store = ledger.store()
    assert store is not None
    started = time.monotonic()
    with pytest.raises(ledger.LedgerUnavailable), ledger.bounded_calls(0.5):
        store.read(0, 64)  # 8 rounds of 8 GETs: 1.8 s unbounded
    assert time.monotonic() - started < 0.5 + 0.2 + 0.5


# --- review round 4 ------------------------------------------------------------------------------
NOT_ENTRIES = {
    "html": b"<html><body>502 Bad Gateway</body></html>",
    "int": b"1",
    "list": b"[1]",
    "null": b"null",
}


def _probe_once() -> str:
    """One health check (`manage.py ledger_status`, a fresh gate): "ok" or its error."""
    from django.core.management import call_command
    from django.core.management.base import CommandError

    GATE.reset()
    try:
        call_command("ledger_status", stdout=io.StringIO())
    except CommandError as exc:
        return str(exc)
    return "ok"


@pytest.mark.django_db
@pytest.mark.parametrize("body", list(NOT_ENTRIES.values()), ids=list(NOT_ENTRIES))
def test_an_old_entry_replaced_by_something_else_is_tampering_for_good(
    s3_ledger, tmp_path, monkeypatch, caplog, body
):
    """LEDGER4-1: an entry verified long ago, replaced by a body that isn't an entry (not
    JSON, an HTML error page, or JSON that isn't an object), was answered "tampered" with a
    WARNING only, or "unavailable" with no log line at all, and nothing was kept: tampering
    disguised as a fault. Found by the pass over the whole chain, it is tampering: kept, and
    the security event at every probe."""
    import logging
    import tempfile

    from arkray.core.management.commands import ledger_status

    found = fresh(3_000)
    s3_ledger["client"] = found.s3
    ledger.mark_applied(found.entries[3_000])
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(ledger_status, "FULL_CHECK_EVERY_S", 0)  # the pass due at every probe
    assert [_probe_once(), _probe_once()] == ["ok", "ok"]  # verified, and a pass completed
    found.s3.put(key(1_500), body)
    with caplog.at_level(logging.WARNING, logger="arkray.core.ledger_gate"):
        answers = [_probe_once(), _probe_once()]
    assert all("tampered" in answer for answer in answers), answers
    events = [r for r in caplog.records if r.getMessage() == "erasure_ledger_tampered"]
    assert len(events) == 2, [r.getMessage() for r in caplog.records]
    assert all(r.levelno == logging.ERROR and getattr(r, "security", False) for r in events)
    kept = ledger_status._load()
    assert kept is not None
    assert kept["tampered"] is True


@pytest.mark.parametrize("body", list(NOT_ENTRIES.values()), ids=list(NOT_ENTRIES))
def test_a_verified_entry_replaced_by_something_else_closes_the_gate_for_good(watched, body):
    """LEDGER4-1: the entry the gate verified last, replaced by a body that isn't an entry:
    tampering (from scratch from here on), not a passing fault."""
    watched.s3.put(key(2_000), body)
    assert GATE.status() == "tampered"
    assert GATE.found_tampering()
    assert GATE.verified_state() is None


@pytest.mark.django_db
@pytest.mark.parametrize("at", [2_500, 3_000], ids=["continued-from", "head"])
def test_the_master_s_refresh_reports_a_verified_entry_that_is_not_json(s3_ledger, caplog, at):
    """LEDGER4-1: the master's refresh meeting the entry it verified last replaced by a body
    that isn't JSON (the entry it continues from, or the head itself when nothing was added)
    logged a WARNING only and kept its state."""
    import logging

    found = fresh(3_000)
    s3_ledger["client"] = found.s3
    GATE.reset()
    GATE.resume(found.state_at(at))
    found.s3.put(key(at), NOT_ENTRIES["html"])
    with caplog.at_level(logging.WARNING, logger="arkray.core.ledger_gate"):
        ledger_gate.refresh()
    events = [r for r in caplog.records if r.getMessage() == "erasure_ledger_tampered"]
    assert events
    assert events[0].levelno == logging.ERROR
    assert GATE.found_tampering()
    assert GATE.verified_state() is None


@pytest.mark.django_db
def test_a_master_that_meets_tampering_at_its_start_says_so(s3_ledger, caplog):
    """LEDGER4-1: `prime` (gunicorn's master and the Celery parent, before forking) meeting
    a tampered entry logged only WARNING `erasure_ledger_prime_failed`."""
    import logging

    found = fresh(3_000)
    data = json.loads(found.s3.bodies[key(2_500)])
    data["subject"] = "00000000-0000-0000-0000-000000000000"
    found.s3.put(key(2_500), json.dumps(data).encode())
    s3_ledger["client"] = found.s3
    GATE.reset()
    with caplog.at_level(logging.WARNING, logger="arkray.core.ledger_gate"):
        ledger_gate.prime()
    events = [r for r in caplog.records if r.getMessage() == "erasure_ledger_tampered"]
    assert events
    assert events[0].levelno == logging.ERROR
    assert getattr(events[0], "security", False)
    assert GATE.found_tampering()  # the processes it forks verify from scratch


class SlowPuts(SlowS3):
    """GETs and LISTs taking `delay`, PUTs `put` (a checkpoint written at the end of a read);
    the moments PUTs began, from `t0`."""

    def __init__(self, s3: FakeS3, delay: float, put_delay: float) -> None:
        super().__init__(s3, delay)
        self.put_delay, self.t0 = put_delay, time.monotonic()
        self.puts: list[float] = []

    def __getattr__(self, name: str) -> Any:
        if name != "put_object":
            return super().__getattr__(name)
        real = getattr(self.s3, name)

        def slow(*args: Any, **kwargs: Any) -> Any:
            self.puts.append(time.monotonic() - self.t0)
            time.sleep(self.put_delay)
            return real(*args, **kwargs)

        return slow


@pytest.mark.django_db
@pytest.mark.parametrize("budget", [3.55, 3.9])
def test_the_master_s_refresh_writes_no_checkpoint_past_its_deadline(
    s3_ledger, monkeypatch, budget
):
    """LEDGER4-2: a read ending just past the deadline and crossing a multiple of 1,000 then
    wrote a checkpoint (2 s a PUT): 6.0 s against 3.55-3.9 s, past "the budget and one short
    call"."""
    found = fresh(2_000)
    GATE.reset()
    GATE.resume(found.state_at(1_968))
    monkeypatch.setattr(ledger_gate, "REFRESH_BUDGET_S", budget)
    before = dict(ledger.CHECKPOINT_FAILURES)
    slow = SlowPuts(found.s3, 0.5, 2.0)
    s3_ledger["client"] = slow
    slow.t0 = started = time.monotonic()
    ledger_gate.refresh()
    took = time.monotonic() - started
    assert took < budget + 0.5 + 0.5, (took, slow.puts)  # one GET at most past it
    assert all(at < budget for at in slow.puts), slow.puts
    assert before == ledger.CHECKPOINT_FAILURES  # out of time: not a store refusing writes


@pytest.mark.django_db
def test_a_checkpointed_entry_that_is_not_json_any_more_is_tampering(s3_ledger, caplog):
    """LEDGER4-1: a checkpoint that verifies says its entry was verified; that entry replaced
    by a body that isn't JSON made the checkpoint count as none, and the read from an older
    state then met it as a passing fault (a WARNING)."""
    import logging

    found = fresh(2_500)
    s3_ledger["client"] = found.s3
    ledger.verified_state()  # checkpoints at 1,000 and 2,000
    GATE.reset()
    GATE.resume(found.state_at(1_000))
    found.s3.put(key(2_000), NOT_ENTRIES["html"])
    with caplog.at_level(logging.WARNING, logger="arkray.core.ledger_gate"):
        ledger_gate.refresh()
    events = [r for r in caplog.records if r.getMessage() == "erasure_ledger_tampered"]
    assert events, [r.getMessage() for r in caplog.records]
    assert GATE.found_tampering()
