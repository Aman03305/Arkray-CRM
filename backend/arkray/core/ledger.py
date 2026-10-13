"""The erasure ledger: what has been erased, kept outside the database so that a restored
backup can't bring it back (docs/privacy.md#restore-safe-erasure, ADR-0032).

A database restore (a dump, point-in-time recovery) returns every row to its state at the
backup, erased people included. So every erasure-like operation also appends an entry here,
in a store the database backups never contain (a directory on its own volume, or an S3
bucket, ideally versioned with Object Lock), and the database remembers how far it has
applied the ledger (`LedgerState`). After a restore the database is *behind* the ledger;
the application refuses API traffic (arkray.core.ledger_gate) until `manage.py
replay_erasures` has re-applied every entry (arkray.privacy.replay).

Entries hold identifiers only, never personal data:

    {"seq": 7, "kind": "lead_erased", "subject": "<uuid>", "at": "<iso time>",
     "prev": "<mac of entry 6>", "mac": "<HMAC-SHA256 of the rest>"}

  (`custom_values_deleted` may add `"owner": "<uuid>"`: only that user's deals; a
  `key_rotated` entry adds `"anchor"`, below. Both are left out when empty, so older
  entries keep their MACs. An `attachment_deleted` entry names one file, or since the
  security review's M3 a batch purged in one transaction, their ids separated by commas
  (`subjects`, at most MAX_SUBJECTS): one entry per batch, not per file. The format is
  unchanged, so a previous release still verifies such an entry; its replay fails on it
  instead, and the API stays closed until this release replays it.)

- **Tamper-evident**: each entry's MAC (ERASURE_LEDGER_KEY, a key of its own, never the
  Django secret) covers its content and the previous entry's MAC, so an entry changed,
  removed or reordered breaks the chain, and `verify()` refuses the whole ledger.
- **Key rotation** (final production audit ARCH-5; docs/runbooks.md#rotate-the-erasure-
  ledger-key): ERASURE_LEDGER_VERIFY_KEYS lists keys accepted besides ERASURE_LEDGER_KEY
  when verifying (never for writing). `rotate_key()` then appends a `key_rotated` entry,
  signed with the current key and naming it by its key id (an HMAC of a fixed label: the
  key itself is never written), whose `anchor` is a SHA-256 digest of every entry before
  it. From there an entry signed with any key used before the checkpoint is refused (a
  retired key that leaked can't add entries), and the entries before it stay verifiable
  through the anchor once their key is removed from the configuration. Nothing is
  re-signed or lost; an interrupted rotation leaves either no checkpoint (run it again) or
  a checkpoint not yet applied (reconciled like any entry).
- **Write-once**: entry N is a file written to a temporary name, synced, then hard-linked to
  its name (which fails if it exists: never a partial entry under an entry's name), or an S3
  object written with `If-None-Match: *`; an existing entry is never overwritten. Appends
  are serialised by a transaction-scoped advisory lock and written *last* in the erasing
  transaction, just before it commits: a failure between the two leaves the ledger ahead of
  the database, which replay resolves by erasing again (the safe direction: the erasure
  was asked for), never the reverse. A deletion carried out by a job (a removed field's
  values, a deleted file's object) is appended by the job, after the request committed, in
  the transaction that removes the data and only when it removes some (security review M3:
  a job that found nothing left appended an entry anyway, so repeating a request grew the
  ledger without limit).
- **Fail closed**: no ledger configured in production is refused at start; a ledger that
  can't be read blocks the API (after LEDGER_MAX_STALE_S of a verified state, or at once in
  a process that never verified it, as after a restore).
- **Constant cost at any size** (security review M3: the head was found by listing the
  whole prefix, one LIST per 1,000 entries, on every gate check and every append, so a
  ledger grown to ~100,000 entries failed readiness): entry names are zero-padded numbers,
  so their lexicographic order is their numeric order. `head(hint)` lists from the entry a
  process last saw (`StartAfter`, a page of `_WINDOW` keys: one LIST, which also shows that
  entry is still there, and one GET), and with no hint, or when that entry is gone (a
  ledger rolled back), finds the last one by galloping and bisecting on small LISTs, each
  answering "is there an entry at or after n" (O(log n), gaps included). A file ledger
  checks names instead of listing its directory. Entries are read only after the last
  verified one (`read(after)`: `StartAfter` on S3).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from urllib.request import url2pathname
from uuid import UUID

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .errors import ServiceUnavailableError

logger = logging.getLogger(__name__)

KEY_ROTATED = "key_rotated"
KINDS = frozenset(
    {"lead_erased", "user_pseudonymised", "custom_values_deleted", "attachment_deleted"}
    | {KEY_ROTATED}
)
# Advisory lock serialising appends.
_LOCK = (0x41524B37, 1)  # "ARK7": its own namespace
_APPEND_ATTEMPTS = 5
_KEY_ID_LABEL = b"arkray-erasure-ledger-key-id"
# A batched entry's subjects (module docstring): ids joined by this, at most MAX_SUBJECTS.
SUBJECT_SEPARATOR = ","
MAX_SUBJECTS = 100
# Keys one LIST asks for when looking for the head (a few hundred bytes each).
_WINDOW = 100
# Entries fetched at once when many are read (a whole chain at a process's start)...
READ_THREADS = 8
# ...and read, checked and dropped this many at a time when a whole chain is verified
# (`verified_state`; review round 1: bounded memory at any size).
_CHUNK = 1_000
# A signed checkpoint of the verified chain is kept every this many entries (`checkpoint`).
CHECKPOINT_EVERY = 1_000
_CHECKPOINT_LABEL = b"arkray-erasure-ledger-checkpoint"


class LedgerError(ServiceUnavailableError):
    """The ledger can't be used as it is (503 through the API: the erasure didn't happen
    and can be retried once the ledger is back)."""

    code = "erasure_ledger_unavailable"
    default_message = "Erasures are unavailable right now. Try again shortly."


class LedgerUnavailable(LedgerError):
    """The store can't be read or written right now."""


class LedgerTampered(LedgerError):
    """An entry's MAC or its link to the previous entry doesn't match."""


class LedgerMalformed(LedgerTampered):
    """A body that isn't JSON at all: an entry corrupted, or a proxy's error page in its
    place. Refused like tampering; a lasting verdict only where an entry was verified before
    (`lasting`): beyond that, the next check reads it again (review round 3, I-5)."""

    def __init__(self, message: str | None = None, *, seq: int | None = None) -> None:
        super().__init__(message)
        self.seq = seq  # the entry's number, when known


def lasting(exc: LedgerTampered, verified: Checked | int | None) -> bool:
    """Whether `exc` is a verdict that stands until the whole chain verifies again: any
    mismatch, and a body that isn't JSON at or below `verified` (an entry this process, or
    the state it continues from, verified before). Only a body beyond anything verified can
    be a passing fault; an old entry replaced by an error page or by JSON that isn't an entry
    is tampering (review round 4, LEDGER4-1: it was answered as a fault, with a WARNING or
    with no log line at all)."""
    if not isinstance(exc, LedgerMalformed):
        return True
    bound = verified.seq if isinstance(verified, Checked) else (verified or 0)
    return exc.seq is not None and exc.seq <= bound


@dataclass(frozen=True, slots=True)
class Entry:
    seq: int
    kind: str
    subject: str
    at: str
    prev: str
    mac: str
    owner: str = ""
    anchor: str = ""

    def content(self) -> dict[str, Any]:
        optional = {"owner": self.owner, "anchor": self.anchor}
        return {
            "seq": self.seq,
            "kind": self.kind,
            "subject": self.subject,
            "at": self.at,
            "prev": self.prev,
            **{name: value for name, value in optional.items() if value},
        }

    def as_json(self) -> bytes:
        return json.dumps({**self.content(), "mac": self.mac}, sort_keys=True).encode()

    def subjects(self) -> list[str]:
        """What the entry names: one id, or a batch's (module docstring)."""
        return [part for part in self.subject.split(SUBJECT_SEPARATOR) if part]


def joined(subjects: Iterable[UUID | str]) -> str:
    """A batch's subject: its ids, sorted, separated by SUBJECT_SEPARATOR (MAX_SUBJECTS)."""
    ids = sorted({str(subject) for subject in subjects})
    if not ids or len(ids) > MAX_SUBJECTS:
        raise ValueError(f"A ledger entry names 1 to {MAX_SUBJECTS} subjects.")
    if any(SUBJECT_SEPARATOR in subject for subject in ids):
        raise ValueError("A ledger subject can't contain the separator.")
    return SUBJECT_SEPARATOR.join(ids)


def _canonical(content: dict[str, Any]) -> bytes:
    return json.dumps(content, sort_keys=True, separators=(",", ":")).encode()


def _mac(content: dict[str, Any], key: str | None = None) -> str:
    secret = (settings.ERASURE_LEDGER_KEY if key is None else key).encode()
    return hmac.new(secret, _canonical(content), hashlib.sha256).hexdigest()


def key_id(key: str) -> str:
    """A key's public name (in `key_rotated` entries): reveals nothing about the key."""
    return hmac.new(key.encode(), _KEY_ID_LABEL, hashlib.sha256).hexdigest()[:16]


def _keys() -> list[str]:
    """The keys an entry may be signed with: the current one first, then those still
    accepted for verification during a rotation."""
    keys = [settings.ERASURE_LEDGER_KEY, *getattr(settings, "ERASURE_LEDGER_VERIFY_KEYS", ())]
    return list(dict.fromkeys(key for key in keys if key))


def _signer(entry: Entry) -> str | None:
    """The id of the configured key whose MAC the entry carries, or None."""
    if not entry.mac.isascii():  # compare_digest would raise on it: altered, not an error
        return None
    content = entry.content()
    for key in _keys():
        if hmac.compare_digest(entry.mac, _mac(content, key)):
            return key_id(key)
    return None


def _entry(data: dict[str, Any]) -> Entry:
    try:
        return Entry(
            seq=int(data["seq"]),
            kind=str(data["kind"]),
            subject=str(data["subject"]),
            at=str(data["at"]),
            prev=str(data["prev"]),
            mac=str(data["mac"]),
            owner=str(data.get("owner") or ""),
            anchor=str(data.get("anchor") or ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerTampered("A ledger entry is malformed.") from exc


# --- bounded store calls ------------------------------------------------------------------------
_limits = threading.local()


class _OutOfTime(LedgerUnavailable):
    """The deadline of `bounded_calls` passed before a store call."""


@contextlib.contextmanager
def bounded_calls(seconds: float) -> Iterator[None]:
    """Every store call made on this thread inside is refused once `seconds` have passed
    (_OutOfTime, before the call: none starts late), and an S3 call waits at most a second to
    connect and two to answer, once (review round 3, LEDGER3-2: gunicorn's master, which
    reaps no worker while it waits). So the whole costs at most `seconds` and one short call."""
    previous = getattr(_limits, "deadline", None)
    _limits.deadline = time.monotonic() + seconds
    try:
        yield
    finally:
        _limits.deadline = previous


def _check_deadline() -> None:
    deadline = getattr(_limits, "deadline", None)
    if deadline is not None and time.monotonic() >= deadline:
        raise _OutOfTime("The ledger read ran out of time.")


# --- stores ----------------------------------------------------------------------------------
class Store(Protocol):
    def read(self, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        """The entries numbered above `after` (the first `limit` of them), in order: every
        one the store holds, so a gap or a stray entry shows."""
        ...

    def last(self, hint: int = 0) -> dict[str, Any] | None:
        """The last entry, found from entry `hint` (one this process saw) when it is still
        there, else from scratch (module docstring: constant, or O(log n), cost)."""
        ...

    def get(self, seq: int) -> dict[str, Any] | None:
        """Entry `seq`, or None."""
        ...

    def create(self, seq: int, body: bytes) -> bool:
        """Write entry `seq` unless it exists (then False)."""
        ...

    def get_checkpoint(self, name: str) -> bytes | None:
        """A verification checkpoint (`checkpoint`), or None."""
        ...

    def put_checkpoint(self, name: str, body: bytes) -> bool:
        """Write a checkpoint unless it exists (then False); write-once like an entry."""
        ...


def _name(seq: int) -> str:
    return f"{seq:012d}.json"


def _past_entries(name: str) -> bool:
    """Whether a name under the ledger's prefix sorts after every entry's (`_name`: digits
    first), so nothing listed after it can be an entry."""
    return name[:1] > "9"


def _numbered(name: str) -> int | None:
    """The number of an entry's file or object name (`_name`), else None."""
    stem, dot, extension = name.partition(".")
    if dot and extension == "json" and len(stem) == 12 and stem.isdigit():
        return int(stem)
    return None


def _as_entry(seq: int, raw: bytes) -> dict[str, Any]:
    """Entry `seq` as stored: JSON naming its own number (LedgerTampered otherwise: an
    entry copied under another's name would otherwise pass for the head)."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or int(data.get("seq", 0)) != seq:
            raise LedgerTampered(f"Ledger entry {seq} isn't the entry its name says.")
    except (TypeError, ValueError) as exc:
        raise LedgerMalformed(f"Ledger entry {seq} isn't valid JSON.", seq=seq) from exc
    return data


Probe = Callable[[int], tuple[int, int, bool]]


def _last_seq(probe: Probe, hint: int, step: int) -> int:
    """The last entry's number (0: none). `probe(n)` looks at the entries numbered n and
    above and answers (the lowest number it saw, the highest, whether the highest is
    certainly the last), with 0s when there is none. From `hint`, when that entry is still
    there: one probe while the head hasn't moved much; from scratch a gallop then a
    bisection, O(log n) probes; when nothing is left at or after it (a ledger rolled back),
    a look just below it, then a bisection under it (review round 1: a rollback found
    quickly, even on a slow store)."""
    if hint > 0:
        lowest, highest, final = probe(hint)
        if lowest:
            return highest if final else _search(probe, highest, step)
        below = max(1, hint - step)
        lowest, highest, final = probe(below)
        if lowest:
            return highest if final else _bisect(probe, highest, hint)
        return _bisect(probe, 0, below)
    lowest, highest, final = probe(1)
    if not lowest:
        return 0
    return highest if final else _search(probe, highest, step)


def _search(probe: Probe, known: int, step: int) -> int:
    """The last entry's number, entry `known` existing: double the distance until nothing
    is there, then bisect between the last entry seen and that point."""
    beyond = 0
    while not beyond:
        lowest, highest, final = probe(known + step)
        if not lowest:
            beyond = known + step
        elif final:
            return highest
        else:
            known, step = highest, step * 2
    return _bisect(probe, known, beyond)


def _bisect(probe: Probe, known: int, beyond: int) -> int:
    """The last entry's number, entry `known` existing (0: none known) and none at or after
    `beyond`."""
    while beyond - known > 1:
        middle = (known + beyond) // 2
        lowest, highest, final = probe(middle)
        if not lowest:
            beyond = middle
        elif final:
            return highest
        else:
            known = highest
    return known


class FileStore:
    """A directory on a volume of its own (never inside a database backup)."""

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)

    def _require(self) -> None:
        # A missing directory is a ledger that can't be read (a volume not mounted), never
        # an empty one, which would read as a ledger rolled back.
        if not self.directory.is_dir():
            raise LedgerUnavailable("The ledger directory doesn't exist.")

    def _entries(self, after: int = 0) -> Iterator[Path]:
        self._require()
        return iter(
            sorted(
                p for p in self.directory.glob("*.json") if p.stem.isdigit() and int(p.stem) > after
            )
        )

    def read(self, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        """The directory's listing: authoritative (a gap or a stray entry shows), read only
        when the head has moved or the whole chain is verified."""
        try:
            paths = list(self._entries(after))
            return [_as_entry(int(path.stem), path.read_bytes()) for path in paths[:limit]]
        except OSError as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc

    def _exists(self, seq: int) -> tuple[int, int, bool]:
        found = (self.directory / _name(seq)).is_file()
        return (seq, seq, False) if found else (0, 0, False)

    def last(self, hint: int = 0) -> dict[str, Any] | None:
        """Found by checking names (one or two from `hint`; O(log n) from scratch), never by
        listing the directory. A gap hides the entries after it from this search: the
        database's position then reads as ahead (closed), and every read lists them."""
        try:
            self._require()
            seq = _last_seq(self._exists, hint, step=1)
            return self.get(seq) if seq else None
        except OSError as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc

    def get(self, seq: int) -> dict[str, Any] | None:
        try:
            self._require()
            return _as_entry(seq, (self.directory / _name(seq)).read_bytes())
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc

    def create(self, seq: int, body: bytes) -> bool:
        """Written in full under a temporary name, synced, then linked to the entry's name
        (an atomic create-if-absent): a full disk or an I/O error leaves no partial entry,
        which would read as a tampered ledger for good (backend review P2). Any OSError is
        LedgerUnavailable: the erasure doesn't happen and can be retried."""
        return self._create(self.directory, _name(seq), body)

    def _create(self, directory: Path, name: str, body: bytes) -> bool:
        path = directory / name
        temporary = directory / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                view = memoryview(body)
                while view:
                    view = view[os.write(descriptor, view) :]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
            self._sync_directory(directory)
            return True
        except OSError as exc:
            raise LedgerUnavailable("The ledger can't be written.") from exc
        finally:
            with contextlib.suppress(OSError):
                os.unlink(temporary)

    @property
    def _checkpoints(self) -> Path:
        # Beside the entries, never among them: "*.json" doesn't look into it.
        return self.directory / "checkpoints"

    def get_checkpoint(self, name: str) -> bytes | None:
        try:
            self._require()
            return (self._checkpoints / name).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc

    def put_checkpoint(self, name: str, body: bytes) -> bool:
        _check_deadline()
        try:
            self._require()
            self._checkpoints.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise LedgerUnavailable("The ledger can't be written.") from exc
        return self._create(self._checkpoints, name, body)

    def _sync_directory(self, directory: Path) -> None:
        """The new name survives a crash (POSIX; Windows can't open a directory)."""
        if not hasattr(os, "O_DIRECTORY"):
            return
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _s3_client(*, connect: float, read: float, attempts: int) -> Any:
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.ERASURE_LEDGER_S3_ENDPOINT_URL or None,
        region_name=settings.ERASURE_LEDGER_S3_REGION or None,
        verify=settings.ERASURE_LEDGER_S3_CA_BUNDLE or None,
        config=Config(
            connect_timeout=connect,
            read_timeout=read,
            retries={"total_max_attempts": attempts},
        ),
    )


class S3Store:
    """An S3 prefix written with conditional puts (`If-None-Match: *`): an entry can't be
    overwritten. Use a bucket of its own with versioning and Object Lock (compliance mode),
    so not even the application's credentials can delete entries."""

    def __init__(self, url: str, client: Any = None) -> None:
        parts = urlsplit(url)
        self.bucket = parts.netloc
        self.prefix = parts.path.strip("/")
        self._client = self._short_client = client

    @property
    def client(self) -> Any:
        """The S3 client. Inside `bounded_calls`, one of its own that waits at most a second
        to connect and two to answer, once (review round 3, LEDGER3-2): a client made
        earlier with the usual timeouts (e.g. by `prime`) never serves a bounded call."""
        if getattr(_limits, "deadline", None) is not None:
            if self._short_client is None:
                self._short_client = _s3_client(connect=1, read=2, attempts=1)
            return self._short_client
        if self._client is None:
            self._client = _s3_client(connect=3, read=10, attempts=2)
        return self._client

    @property
    def _listed(self) -> str:
        # The bucket's root when there's no prefix (s3://bucket): "/" would match nothing.
        return f"{self.prefix}/" if self.prefix else ""

    def _key(self, seq: int) -> str:
        return f"{self.prefix}/{_name(seq)}" if self.prefix else _name(seq)

    def _list(self, after: int, page_size: int) -> Iterator[tuple[list[str], bool]]:
        """Pages of the keys after entry `after`'s (all of them from 0) in lexicographic
        order, the numbers' order: each page's keys, and whether more follow."""
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": self._listed}
        if page_size < 1000:
            kwargs["MaxKeys"] = page_size
        if after > 0:
            kwargs["StartAfter"] = self._key(after)
        while True:
            _check_deadline()
            page = self.client.list_objects_v2(**kwargs)
            more = bool(page.get("IsTruncated"))
            yield [item["Key"] for item in page.get("Contents", [])], more
            if not more:
                return
            kwargs.pop("StartAfter", None)
            kwargs["ContinuationToken"] = page["NextContinuationToken"]

    def _keys(self, after: int = 0, limit: int | None = None) -> list[str]:
        """Every entry's key after entry `after`: listed from there (`StartAfter`), never
        from the start. Any all-digit name counts, as it always has, so a stray entry shows
        when the chain is checked."""
        keys: list[str] = []
        prefix = self._listed
        for page, _ in self._list(after, 1000):
            keys += [
                k
                for k in page
                if k.endswith(".json")
                and "/" not in k[len(prefix) :]
                and k[len(prefix) : -5].isdigit()
                and int(k[len(prefix) : -5]) > after
            ]
            if limit is not None and len(keys) >= limit:
                break
            if page and _past_entries(page[-1][len(prefix) :]):
                break  # the rest of the listing sorts after every entry
        found = sorted(keys, key=lambda k: int(k[len(prefix) : -5]))
        return found if limit is None else found[:limit]

    def _probe(self, seq: int) -> tuple[int, int, bool]:
        """One LIST of up to _WINDOW keys from entry `seq`'s on (module docstring): the
        lowest and highest entry numbers in it, and whether nothing follows. A page of other
        objects only (none in a bucket of its own) is followed by the next one; a key that
        sorts after every entry's name ends the search (review round 2, LEDGER2-2: other
        objects after the entries cost every head a page each)."""
        prefix = self._listed
        for page, more in self._list(seq - 1, _WINDOW):
            rests = [k[len(prefix) :] for k in page if k.startswith(prefix)]
            numbers = [n for n in (_numbered(r) for r in rests) if n is not None and n >= seq]
            final = not more or any(_past_entries(r) for r in rests)
            if numbers:
                return min(numbers), max(numbers), final
            if final:
                break
        return 0, 0, True

    def _get(self, key: str) -> dict[str, Any]:
        _check_deadline()
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        # Checked as `get` checks an entry (review round 4, LEDGER4-1: JSON that isn't an
        # object read as "unavailable", without a log line).
        return _as_entry(int(key.rsplit("/", 1)[-1][:-5]), body)

    def _get_all(self, keys: list[str]) -> list[dict[str, Any]]:
        """In order; READ_THREADS at a time when there are more, a chunk of _CHUNK keys
        submitted at a time (review round 1: `map` over every key at once queued a future
        per entry, 495 MiB at 200,000 entries)."""
        if len(keys) <= READ_THREADS:
            return [self._get(key) for key in keys]
        _ = self.client  # created once, before any thread uses it
        deadline = getattr(_limits, "deadline", None)

        def get(key: str) -> dict[str, Any]:
            _limits.deadline = deadline  # bounded_calls holds on the pool's threads too
            return self._get(key)

        found: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=READ_THREADS) as pool:
            for start in range(0, len(keys), _CHUNK):
                found += pool.map(get, keys[start : start + _CHUNK])
        return found

    def read(self, after: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        try:
            return self._get_all(self._keys(after, limit))
        except LedgerError:
            raise
        except ValueError as exc:
            raise LedgerMalformed("A ledger entry isn't valid JSON.") from exc
        except Exception as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc

    def last(self, hint: int = 0) -> dict[str, Any] | None:
        try:
            seq = _last_seq(self._probe, hint, step=_WINDOW)
            return self.get(seq) if seq else None
        except LedgerError:
            raise
        except ValueError as exc:
            raise LedgerMalformed("A ledger entry isn't valid JSON.") from exc
        except Exception as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc

    def get(self, seq: int) -> dict[str, Any] | None:
        from botocore.exceptions import ClientError

        _check_deadline()
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=self._key(seq))["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "") in {"NoSuchKey", "404"}:
                return None
            raise LedgerUnavailable("The ledger can't be read.") from exc
        except Exception as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc
        return _as_entry(seq, body)

    def create(self, seq: int, body: bytes) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(seq),
                Body=body,
                IfNoneMatch="*",
                ContentType="application/json",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"}:
                return False
            raise LedgerUnavailable("The ledger can't be written.") from exc
        except Exception as exc:
            raise LedgerUnavailable("The ledger can't be written.") from exc
        return True

    def _checkpoint_key(self, name: str) -> str:
        """Beside the entries, never among them (review round 2, LEDGER2-2): a sibling prefix
        (`<prefix>.checkpoints/`) that no listing of the entries' prefix returns, or at a
        bucket's root `_checkpoints/`, which sorts after every entry and ends a listing
        (`_past_entries`). Earlier releases ignore both (names with "/")."""
        return f"{self.prefix}.checkpoints/{name}" if self.prefix else f"_checkpoints/{name}"

    def get_checkpoint(self, name: str) -> bytes | None:
        from botocore.exceptions import ClientError

        _check_deadline()
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._checkpoint_key(name))
            return bytes(response["Body"].read())
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "") in {"NoSuchKey", "404"}:
                return None
            raise LedgerUnavailable("The ledger can't be read.") from exc
        except Exception as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc

    def put_checkpoint(self, name: str, body: bytes) -> bool:
        from botocore.exceptions import ClientError

        _check_deadline()  # none begins past the deadline (review round 4, LEDGER4-2)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._checkpoint_key(name),
                Body=body,
                IfNoneMatch="*",
                ContentType="application/json",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"}:
                return False
            raise LedgerUnavailable("The ledger can't be written.") from exc
        except Exception as exc:
            raise LedgerUnavailable("The ledger can't be written.") from exc
        return True


def store() -> Store | None:
    """The configured store, or None when the ledger is off (development, tests)."""
    url = settings.ERASURE_LEDGER_URL
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme == "file":
        return FileStore(url2pathname(parts.path))
    if parts.scheme == "s3":
        return S3Store(url)
    raise LedgerError("ERASURE_LEDGER_URL must be file:///... or s3://bucket/prefix.")


def enabled() -> bool:
    return store() is not None


# --- reading -------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Checked:
    """How far a chain has been verified: enough to check what is appended after it
    without reading it again (the store is write-once, so a verified prefix stays so)."""

    seq: int = 0
    mac: str = ""
    # SHA-256 over every entry so far (what a `key_rotated` entry anchors).
    digest: str = ""
    # Keys (by id) that signed entries before the last rotation: refused from there on.
    retired: frozenset[str] = frozenset()
    # Keys that signed entries since the last rotation.
    signers: frozenset[str] = frozenset()
    # The key the last rotation introduced ("" before any).
    key: str = ""
    # Partway through a verification spread over several reads (`check(final=False)`): the
    # first entry signed with a key no longer configured, which a later rotation's anchor
    # must still vouch for (0: none). A state with one pending is not a verified chain.
    pending: int = 0


def _digest(previous: str, entry: Entry) -> str:
    body = _canonical({**entry.content(), "mac": entry.mac})
    return hashlib.sha256(previous.encode() + body).hexdigest()


def check(
    entries: list[dict[str, Any]],
    start: Checked | None = None,
    *,
    final: bool = True,
    marks: list[Checked] | None = None,
) -> tuple[list[Entry], Checked]:
    """`entries`, continuing the chain verified up to `start`: numbered on, each linked to
    the one before, of a known kind, and signed with a configured key, or anchored by a
    later `key_rotated` entry (a retired key no longer configured). Returns them with the
    state reached. LedgerTampered otherwise. With `final=False` (a slice of a longer
    verification) an entry still waiting for a later anchor is carried in the state
    (`pending`) instead of refused: the slice that ends the verification decides. `marks`
    collects the verified state at each multiple of CHECKPOINT_EVERY (none pending)."""
    state = start or Checked()
    checked: list[Entry] = []
    pending = state.pending  # signed with no configured key: a later anchor must cover it
    for expected, data in enumerate(entries, start=state.seq + 1):
        entry = _entry(data)
        if entry.seq != expected:
            raise LedgerTampered(f"Ledger entry {expected} is missing or out of order.")
        if entry.prev != state.mac:
            raise LedgerTampered(f"Ledger entry {entry.seq} isn't linked to the one before.")
        if entry.kind not in KINDS:
            raise LedgerTampered(f"Ledger entry {entry.seq} has an unknown kind.")
        signer = _signer(entry)
        retired, signers = state.retired, state.signers
        if entry.kind == KEY_ROTATED:
            # A retired key never comes back, not even through a rotation signed with it
            # (a leaked old key still configured during the transition; review P2).
            if entry.subject in retired or (signer is not None and signer in retired):
                raise LedgerTampered(f"Ledger entry {entry.seq} brings back a retired key.")
            # Its key retired since (a later rotation): a later anchor must cover it.
            if signer is None:
                pending = pending or entry.seq
            elif signer != entry.subject:
                raise LedgerTampered(
                    f"Ledger entry {entry.seq} isn't signed with the key it introduces."
                )
            if not hmac.compare_digest(entry.anchor, state.digest):
                raise LedgerTampered(
                    f"Ledger entry {entry.seq} doesn't match the entries before it."
                )
            if signer is not None:  # trusted: it vouches for everything before it
                pending = 0
            new_key = entry.subject
            retired, signers = (retired | signers) - {new_key}, frozenset({new_key})
        elif signer is None:
            pending = pending or entry.seq
        elif signer in retired:
            raise LedgerTampered(f"Ledger entry {entry.seq} is signed with a retired key.")
        else:
            signers = signers | {signer}
        checked.append(entry)
        state = Checked(
            seq=entry.seq,
            mac=entry.mac,
            digest=_digest(state.digest, entry),
            retired=retired,
            signers=signers,
            key=entry.subject if entry.kind == KEY_ROTATED else state.key,
            pending=pending,
        )
        if marks is not None and not pending and entry.seq % CHECKPOINT_EVERY == 0:
            marks.append(state)
    if final and pending:
        raise LedgerTampered(
            f"Ledger entry {pending} has been altered, or its key isn't configured."
        )
    return checked, state


def verify(
    entries: list[dict[str, Any]] | None = None, *, after: Checked | None = None
) -> list[Entry]:
    """Every entry, checked (`check`): numbered 1, 2, 3, ..., each MAC right and each linked
    to the one before. LedgerTampered otherwise; LedgerUnavailable if it can't be read.

    `after`: only the entries after a prefix already verified, checked to continue its
    chain (the gate's incremental check; replay always checks the whole chain first)."""
    return verified(entries, after=after)[0]


def _still_there(found: Store, after: Checked) -> None:
    """Continuing from `after`, the entry it ends with must still be there, unchanged: one
    deleted or replaced since it was verified, with the entries after it intact, is
    tampering too (security review M3: those are read from it, no longer from the start)."""
    if after.seq > 0:
        try:
            kept = found.get(after.seq)
        except LedgerMalformed as exc:  # verified before: tampering (review round 4)
            raise LedgerTampered(f"Ledger entry {after.seq} isn't an entry any more.") from exc
        if kept is None or kept.get("mac") != after.mac:
            raise LedgerTampered(f"Ledger entry {after.seq} changed after it was verified.")


def verified(
    entries: list[dict[str, Any]] | None = None, *, after: Checked | None = None
) -> tuple[list[Entry], Checked]:
    """`verify`, with the state reached (to continue from later)."""
    found = store()
    if found is None:
        return [], after or Checked()
    if entries is None and after is not None:
        _still_there(found, after)
    if entries is not None:
        return check(entries, after)
    marks: list[Checked] = []
    checked, state = check(found.read(after.seq if after else 0), after, marks=marks)
    _keep_checkpoints(found, marks)
    return checked, state


def verified_state(after: Checked | None = None) -> Checked:
    """`verified`, keeping no entry: the chain read, checked and dropped _CHUNK entries at a
    time (review round 1: a process's start held every entry at once, ~2.5 KiB each), with
    a checkpoint kept at each multiple of CHECKPOINT_EVERY it passes."""
    found = store()
    state = after or Checked()
    if found is None:
        return state
    if after is not None:
        _still_there(found, after)
    while True:
        rows = found.read(state.seq, _CHUNK)
        if not rows:
            break
        marks: list[Checked] = []
        _, state = check(rows, state, final=False, marks=marks)
        _keep_checkpoints(found, marks)
        if len(rows) < _CHUNK:
            break
    if state.pending:
        raise LedgerTampered(
            f"Ledger entry {state.pending} has been altered, or its key isn't configured."
        )
    return state


def verified_until(start: Checked, upto: int, limit: int) -> Checked:
    """The chain verified on from `start` toward entry `upto`, at most `limit` entries at a
    time: a whole verification spread over several calls (manage.py ledger_status), each
    bounded, whatever the entries (review round 1: after a key rotation that dropped the old
    key, a slice failed and the rest was read at once). An entry still waiting for a later
    rotation's anchor is carried in the state (`pending`); the call that reaches `upto`
    refuses it. LedgerTampered when an entry up to `upto` is missing or doesn't verify."""
    found = store()
    if found is None or start.seq >= upto:
        return start
    count = min(limit, upto - start.seq)
    marks: list[Checked] = []
    _, state = check(found.read(start.seq, count), start, final=False, marks=marks)
    if state.seq < start.seq + count:
        raise LedgerTampered(f"Ledger entry {state.seq + 1} is missing.")
    _keep_checkpoints(found, marks)
    if state.seq >= upto and state.pending:
        raise LedgerTampered(
            f"Ledger entry {state.pending} has been altered, or its key isn't configured."
        )
    return state


# --- checkpoints -----------------------------------------------------------------------------
# A process's start verified the whole chain: one GET per entry, 33 s at 10,000 entries on
# a 25 ms store, past a health check's 30 s (review round 1, LEDGER-2 b). A verification
# keeps, at each multiple of CHECKPOINT_EVERY it passes, a checkpoint in the ledger's own
# store (`checkpoints/<seq>.<key id>.json`, written once like an entry): the state verified
# there (`Checked`: a number, MACs, digests and key ids, never an entry's subject or a key),
# signed with ERASURE_LEDGER_KEY under a label of its own. A process starting (prime, the
# first health probe of a container) begins from the newest one at or below the head that
# verifies: its signature with the current key, and the entry it ends with still there with
# the same MAC; it then reads only the entries after it. Only the key's holder can write a
# checkpoint that verifies, and the key's holder could write entries too. Nothing about a
# restore changes: the gate still compares the database's position with the head. What a
# start no longer does is re-read the entries before the checkpoint; every Celery
# container's health check still verifies the whole chain from scratch at least hourly
# (manage.py ledger_status), and so do `replay_erasures`, the key rotation and preflight.
def _checkpoint_name(seq: int, signer: str) -> str:
    return f"{seq:012d}.{signer}.json"


def _checkpoint_content(state: Checked) -> dict[str, Any]:
    return {
        "seq": state.seq,
        "mac": state.mac,
        "digest": state.digest,
        "retired": sorted(state.retired),
        "signers": sorted(state.signers),
        "key": state.key,
    }


def _checkpoint_sig(content: dict[str, Any]) -> str:
    secret = settings.ERASURE_LEDGER_KEY.encode()
    return hmac.new(
        secret, _CHECKPOINT_LABEL + b"\n" + _canonical(content), hashlib.sha256
    ).hexdigest()


def _keep_checkpoints(found: Store, marks: list[Checked]) -> None:
    """A checkpoint for each verified state in `marks`, unless one is there already. Best
    effort: a store that refuses one costs a later start a longer read, nothing else."""
    signer = key_id(settings.ERASURE_LEDGER_KEY)
    for state in marks:
        content = _checkpoint_content(state)
        body = json.dumps({**content, "sig": _checkpoint_sig(content)}, sort_keys=True).encode()
        try:
            found.put_checkpoint(_checkpoint_name(state.seq, signer), body)
        except _OutOfTime:
            return  # bounded_calls: a later verification writes it; the store refused nothing
        except LedgerError as exc:
            _checkpoints_unavailable("write", exc)
            return


# Checkpoints the store refused, in this process (logs and the health probe; review round 2,
# LEDGER2-7: an IAM policy without the checkpoints' prefix).
CHECKPOINT_FAILURES = {"read": 0, "write": 0}


def _checkpoints_unavailable(operation: str, exc: Exception) -> None:
    """A checkpoint the store wouldn't read or write: there is none, as far as a start is
    concerned (it reads more), never an unavailable ledger. Logged for the operator: the
    ledger's credentials need read and write on the checkpoints' prefix too."""
    CHECKPOINT_FAILURES[operation] += 1
    logger.warning(
        "erasure_ledger_checkpoint_unavailable",
        extra={"operation": operation, "error": type(exc).__name__},
    )


def _load_checkpoint(found: Store, seq: int) -> Checked | None:
    try:
        raw = found.get_checkpoint(_checkpoint_name(seq, key_id(settings.ERASURE_LEDGER_KEY)))
    except _OutOfTime:
        raise
    except LedgerError as exc:
        _checkpoints_unavailable("read", exc)
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        content = {k: data[k] for k in ("seq", "mac", "digest", "retired", "signers", "key")}
        if not hmac.compare_digest(str(data["sig"]).encode(), _checkpoint_sig(content).encode()):
            return None
        state = Checked(
            seq=int(content["seq"]),
            mac=str(content["mac"]),
            digest=str(content["digest"]),
            retired=frozenset(str(k) for k in content["retired"]),
            signers=frozenset(str(k) for k in content["signers"]),
            key=str(content["key"]),
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if state.seq != seq:
        return None
    try:
        entry = found.get(seq)
    except LedgerMalformed as exc:
        # A checkpoint that verifies says this entry was verified: tampering, not a fault
        # (review round 4, LEDGER4-1).
        raise LedgerTampered(f"Ledger entry {seq} isn't an entry any more.") from exc
    except LedgerTampered:
        return None  # the verification from the start says what is wrong
    if entry is None or entry.get("mac") != state.mac:
        return None  # not this ledger's chain (any more): ignored, never trusted
    return state


def verified_for(start: Checked | None, budget_s: float, *, chunk: int = _CHUNK) -> Checked:
    """The chain verified on from `start` toward the head, _CHUNK entries at a time, for at
    most about `budget_s`: the furthest state reached that is a verified chain (nothing
    pending), `start` if none. Bounded work for a process that must not wait long (gunicorn's
    master before each fork, review round 2, LEDGER2-3)."""
    found = store()
    state = best = start or Checked()
    if found is None:
        return best
    if start is not None:
        _still_there(found, start)
    deadline = time.monotonic() + budget_s
    ended = False
    while time.monotonic() < deadline:
        try:
            rows = found.read(state.seq, chunk)
        except _OutOfTime:
            break  # bounded_calls: what is verified so far is kept
        if rows:
            marks: list[Checked] = []
            _, state = check(rows, state, final=False, marks=marks)
            _keep_checkpoints(found, marks)
            if not state.pending:
                best = state
        if len(rows) < chunk:
            ended = True
            break
    if ended and state.pending:
        raise LedgerTampered(
            f"Ledger entry {state.pending} has been altered, or its key isn't configured."
        )
    return best


def checkpoint(hint: int = 0) -> Checked | None:
    """The newest checkpoint at or below the head that verifies (above), or None: where a
    starting process may begin verifying. Looks at the last three multiples. One the store
    won't hand over counts as none (logged: `_checkpoints_unavailable`)."""
    found = store()
    if found is None:
        return None
    last = head(hint)
    if last is None:
        return None
    newest = last.seq - last.seq % CHECKPOINT_EVERY
    for seq in range(newest, max(0, newest - 3 * CHECKPOINT_EVERY), -CHECKPOINT_EVERY):
        state = _load_checkpoint(found, seq)
        if state is not None:
            return state
    return None


def head(hint: int = 0) -> Entry | None:
    """The last entry (its MAC checked against the configured keys), or None for an empty
    ledger. `hint`: an entry this process saw (the last it verified, or the one the
    database applied), from which the head costs one small read (module docstring)."""
    found = store()
    if found is None:
        return None
    data = found.last(hint)
    if data is None:
        return None
    entry = _entry(data)
    if _signer(entry) is None:
        raise LedgerTampered(f"Ledger entry {entry.seq} has been altered.")
    return entry


def entry_at(seq: int) -> Entry | None:
    """Entry `seq` as stored (not verified on its own), or None."""
    found = store()
    if found is None or seq < 1:
        return None
    data = found.get(seq)
    return _entry(data) if data is not None else None


def applied() -> tuple[int, str]:
    """How far this database has applied the ledger (LedgerState)."""
    from .models import LedgerState

    state = LedgerState.objects.filter(pk=1).values_list("applied_seq", "applied_mac").first()
    return (int(state[0]), str(state[1])) if state else (0, "")


def appending() -> bool:
    """Whether an erasure is appending right now (it holds the append lock): its entry may
    be written while its transaction, which moves LedgerState, hasn't committed yet."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", list(_LOCK))
        row = cursor.fetchone()
    return not (row and row[0])


def lock() -> None:
    """Take the append lock until the current transaction ends."""
    if not connection.in_atomic_block:
        raise RuntimeError("The ledger lock is held by a transaction.")
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", list(_LOCK))


def mark_applied(entry: Entry | None) -> None:
    from .models import LedgerState

    LedgerState.objects.update_or_create(
        pk=1,
        defaults={
            "applied_seq": entry.seq if entry else 0,
            "applied_mac": entry.mac if entry else "",
            "updated_at": timezone.now(),
        },
    )


# --- writing -------------------------------------------------------------------------------------
def append(
    kind: str,
    subject: UUID | str,
    *,
    owner: UUID | str | None = None,
    at: datetime | None = None,
) -> Entry | None:
    """Record an erasure, inside the transaction that performs it (module docstring). None
    when the ledger is off. LedgerUnavailable propagates: the erasure doesn't happen."""
    if kind not in KINDS or kind == KEY_ROTATED:
        raise ValueError(f"Unknown ledger kind {kind!r}.")
    return _append(kind, str(subject), owner="" if owner is None else str(owner), at=at)


def rotate_key() -> Entry | None:
    """Anchor the ledger to the current ERASURE_LEDGER_KEY (module docstring): a
    `key_rotated` entry, unless the last rotation already named this key (None). Run once
    every process signs with the new key and still accepts the old one
    (ERASURE_LEDGER_VERIFY_KEYS). Checks the whole chain first: an entry it can't verify
    stops the rotation (LedgerTampered), so nothing unverified is ever anchored."""
    if store() is None:
        raise LedgerError("No erasure ledger is configured (ERASURE_LEDGER_URL).")
    current = key_id(settings.ERASURE_LEDGER_KEY)
    with transaction.atomic():
        lock()
        _, state = verified()
        if state.key == current and state.signers == frozenset({current}):
            return None  # already anchored to this key, which alone signed since
        last = head(state.seq)
        if (last.seq if last else 0) != state.seq:
            raise LedgerUnavailable("The ledger changed during the rotation; run it again.")
        return _append(KEY_ROTATED, current, anchor=state.digest, attempts=1)


def _append(
    kind: str,
    subject: str,
    *,
    owner: str = "",
    anchor: str = "",
    at: datetime | None = None,
    attempts: int = _APPEND_ATTEMPTS,
) -> Entry | None:
    found = store()
    if found is None:
        return None
    if not connection.in_atomic_block:
        raise RuntimeError("Ledger entries are appended inside the erasing transaction.")
    lock()
    moment = (at or timezone.now()).isoformat()
    # An anchored entry is written once: on a moved head its anchor would be stale.
    for _ in range(attempts):
        # Where this database stands is where the head normally is: one small read.
        applied_seq, applied_mac = applied()
        last = head(applied_seq)
        _require_on_chain(applied_seq, applied_mac, last)
        draft = Entry(
            seq=(last.seq if last else 0) + 1,
            kind=kind,
            subject=subject,
            at=moment,
            prev=last.mac if last else "",
            mac="",
            owner=owner,
            anchor=anchor,
        )
        entry = Entry(**{**draft.content(), "mac": _mac(draft.content())})
        if found.create(entry.seq, entry.as_json()):
            # This database has applied everything up to the new entry, unless it was
            # already behind (a restore not yet replayed): then it stays behind.
            if last is None or (applied_seq, applied_mac) == (last.seq, last.mac):
                mark_applied(entry)
            return entry
    raise LedgerUnavailable("The ledger kept changing under this append; try again.")


def _require_on_chain(applied_seq: int, applied_mac: str, last: Entry | None) -> None:
    """An append continues the chain this database applied, or nothing: the entry it last
    applied must be in the ledger with the same MAC (review round 3, LEDGER3-1: on a ledger
    rolled back below the database, appends regrew it and the one reaching the database's
    number moved its position onto the replaced chain, the lost erasures' entries accepted
    without a word). Otherwise the erasure stops (503, nothing changed) and the gate stays
    closed ("ahead" or "diverged") until an operator restores the right ledger."""
    if not applied_seq:
        return
    if last is None or last.seq < applied_seq:
        raise LedgerTampered(
            f"This database applied {applied_seq} ledger entries but the ledger holds"
            f" {last.seq if last else 0}: restore the right ledger before erasing again."
        )
    held = last if last.seq == applied_seq else entry_at(applied_seq)
    if held is None or held.mac != applied_mac:
        raise LedgerTampered(
            f"Ledger entry {applied_seq} isn't the one this database applied: restore the"
            " right ledger before erasing again."
        )
