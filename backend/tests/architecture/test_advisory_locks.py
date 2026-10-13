"""Every advisory lock has a namespace of its own (review of 73ff1e3, round 1, QUEUES-4).

Transaction advisory locks on two int4 keys are told apart by their first key, the namespace
("ARK" and a letter, `0x41524Bxx`); the second key is a digest of what is locked. The daily
write budget's lock reused the legal-hold barrier's namespace ("ARKB"): a person whose id
digested to the barrier's second key (0) would have taken the barrier, exclusively, on every
activity they created, and the barrier's lock-order check could then refuse a reader. This
scans the code for every namespace constant and fails when one names two different locks.
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
# A name bound to an "ARK?" constant, alone or as the first key of a (namespace, key) pair.
CONSTANT = re.compile(r"^(_?[A-Z][A-Z_]*)\s*=\s*\(?\s*(0x41524B[0-9A-Fa-f]{2})\b", re.MULTILINE)
# Single-key locks: hashtextextended(<text>, <seed>), the seed being the namespace.
SEED = re.compile(r"hashtextextended\([^,]+,\s*(\d+)\)")
# Namespaces allowed to name two locks: none. "ARK3" was shared before this check existed (a
# sign-in's per-source count and a pipeline creation per owner); the sign-in lock moved to
# "ARK5" (identity.throttling).
KNOWN: dict[int, set[str]] = {}


def sources() -> list[Path]:
    return [
        path
        for folder in ("arkray", "config")
        for path in sorted((BACKEND / folder).rglob("*.py"))
        if "tests" not in path.parts and "migrations" not in path.parts
    ]


def namespaces() -> dict[int, set[str]]:
    found: dict[int, set[str]] = {}
    for path in sources():
        text = path.read_text(encoding="utf-8")
        where = path.relative_to(BACKEND).as_posix()
        for name, value in CONSTANT.findall(text):
            found.setdefault(int(value, 16), set()).add(f"{where}:{name}")
    return found


def test_the_scan_finds_the_locks_it_should():
    found = {owner for owners in namespaces().values() for owner in owners}
    for expected in (
        "arkray/core/holds.py:_BARRIER",
        "arkray/core/holds.py:_LOCK_NAMESPACE",
        "arkray/core/idempotency.py:_LOCK_NAMESPACE",
        "arkray/core/ledger.py:_LOCK",
        "arkray/activities/attachments.py:_QUOTA_LOCK",
        "arkray/activities/services.py:_BUDGET_LOCK",
    ):
        assert expected in found, expected


def test_every_two_key_namespace_names_one_lock():
    shared = {
        value: owners
        for value, owners in namespaces().items()
        if len(owners) > 1 and owners != KNOWN.get(value)
    }
    assert shared == {}, {hex(value): sorted(owners) for value, owners in shared.items()}


def test_the_daily_write_budget_has_its_own_namespace():
    from arkray.activities import services
    from arkray.core import holds

    assert holds._BARRIER[0] != services._BUDGET_LOCK
    assert services._BUDGET_LOCK not in KNOWN


def test_single_key_locks_use_their_own_seeds():
    """`hashtextextended(<text>, <seed>)` keys: one seed per kind of lock (ai.indexing's
    per-source lock, ai.service's bulkheads, the erasure lock)."""
    seeds: dict[str, set[str]] = {}
    for path in sources():
        for seed in SEED.findall(path.read_text(encoding="utf-8")):
            seeds.setdefault(seed, set()).add(path.relative_to(BACKEND).as_posix())
    assert seeds, "the scan found no single-key lock"
    assert all(len(files) == 1 for files in seeds.values()), seeds
