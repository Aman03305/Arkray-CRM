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

- **Tamper-evident**: each entry's MAC (ERASURE_LEDGER_KEY, a key of its own, never the
  Django secret) covers its content and the previous entry's MAC, so an entry changed,
  removed or reordered breaks the chain, and `verify()` refuses the whole ledger.
- **Write-once**: entry N is a file written to a temporary name, synced, then hard-linked to
  its name (which fails if it exists: never a partial entry under an entry's name), or an S3
  object written with `If-None-Match: *`; an existing entry is never overwritten. Appends
  are serialised by a transaction-scoped advisory lock and written *last* in the erasing
  transaction, just before it commits: a failure between the two leaves the ledger ahead of
  the database, which replay resolves by erasing again (the safe direction: the erasure
  was asked for), never the reverse. A deletion carried out by a job (a removed field's
  values, a deleted file's object) is appended by the job, after the request committed.
- **Fail closed**: no ledger configured in production is refused at start; a ledger that
  can't be read blocks the API (after LEDGER_MAX_STALE_S of a verified state, or at once in
  a process that never verified it, as after a restore).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Iterator
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

KINDS = frozenset(
    {"lead_erased", "user_pseudonymised", "custom_values_deleted", "attachment_deleted"}
)
# Advisory lock serialising appends.
_LOCK = (0x41524B37, 1)  # "ARK7": its own namespace
_APPEND_ATTEMPTS = 5


class LedgerError(ServiceUnavailableError):
    """The ledger can't be used as it is (503 through the API: the erasure didn't happen
    and can be retried once the ledger is back)."""

    code = "erasure_ledger_unavailable"
    default_message = "Erasures are unavailable right now. Try again shortly."


class LedgerUnavailable(LedgerError):
    """The store can't be read or written right now."""


class LedgerTampered(LedgerError):
    """An entry's MAC or its link to the previous entry doesn't match."""


@dataclass(frozen=True, slots=True)
class Entry:
    seq: int
    kind: str
    subject: str
    at: str
    prev: str
    mac: str

    def content(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "subject": self.subject,
            "at": self.at,
            "prev": self.prev,
        }

    def as_json(self) -> bytes:
        return json.dumps({**self.content(), "mac": self.mac}, sort_keys=True).encode()


def _mac(content: dict[str, Any]) -> str:
    key = settings.ERASURE_LEDGER_KEY.encode()
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def _entry(data: dict[str, Any]) -> Entry:
    try:
        return Entry(
            seq=int(data["seq"]),
            kind=str(data["kind"]),
            subject=str(data["subject"]),
            at=str(data["at"]),
            prev=str(data["prev"]),
            mac=str(data["mac"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LedgerTampered("A ledger entry is malformed.") from exc


# --- stores ----------------------------------------------------------------------------------
class Store(Protocol):
    def read(self, after: int = 0) -> list[dict[str, Any]]:
        """The entries numbered above `after`, in order."""
        ...

    def last(self) -> dict[str, Any] | None: ...

    def create(self, seq: int, body: bytes) -> bool:
        """Write entry `seq` unless it exists (then False)."""
        ...


def _name(seq: int) -> str:
    return f"{seq:012d}.json"


class FileStore:
    """A directory on a volume of its own (never inside a database backup)."""

    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)

    def _entries(self, after: int = 0) -> Iterator[Path]:
        if not self.directory.is_dir():
            raise LedgerUnavailable("The ledger directory doesn't exist.")
        return iter(
            sorted(
                p for p in self.directory.glob("*.json") if p.stem.isdigit() and int(p.stem) > after
            )
        )

    def read(self, after: int = 0) -> list[dict[str, Any]]:
        try:
            return [json.loads(path.read_bytes()) for path in self._entries(after)]
        except OSError as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc
        except ValueError as exc:
            raise LedgerTampered("A ledger entry isn't valid JSON.") from exc

    def last(self) -> dict[str, Any] | None:
        try:
            names = list(self._entries())
            return json.loads(names[-1].read_bytes()) if names else None
        except OSError as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc
        except ValueError as exc:
            raise LedgerTampered("A ledger entry isn't valid JSON.") from exc

    def create(self, seq: int, body: bytes) -> bool:
        """Written in full under a temporary name, synced, then linked to the entry's name
        (an atomic create-if-absent): a full disk or an I/O error leaves no partial entry,
        which would read as a tampered ledger for good (backend review P2). Any OSError is
        LedgerUnavailable: the erasure doesn't happen and can be retried."""
        path = self.directory / _name(seq)
        temporary = self.directory / f".{_name(seq)}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
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
            self._sync_directory()
            return True
        except OSError as exc:
            raise LedgerUnavailable("The ledger can't be written.") from exc
        finally:
            with contextlib.suppress(OSError):
                os.unlink(temporary)

    def _sync_directory(self) -> None:
        """The new name survives a crash (POSIX; Windows can't open a directory)."""
        if not hasattr(os, "O_DIRECTORY"):
            return
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class S3Store:
    """An S3 prefix written with conditional puts (`If-None-Match: *`): an entry can't be
    overwritten. Use a bucket of its own with versioning and Object Lock (compliance mode),
    so not even the application's credentials can delete entries."""

    def __init__(self, url: str, client: Any = None) -> None:
        parts = urlsplit(url)
        self.bucket = parts.netloc
        self.prefix = parts.path.strip("/")
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3  # type: ignore[import-untyped]
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=settings.ERASURE_LEDGER_S3_ENDPOINT_URL or None,
                region_name=settings.ERASURE_LEDGER_S3_REGION or None,
                verify=settings.ERASURE_LEDGER_S3_CA_BUNDLE or None,
                config=Config(
                    connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 2}
                ),
            )
        return self._client

    def _key(self, seq: int) -> str:
        return f"{self.prefix}/{_name(seq)}" if self.prefix else _name(seq)

    def _keys(self, after: int = 0) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        # The bucket's root when there's no prefix (s3://bucket): "/" would match nothing.
        prefix = f"{self.prefix}/" if self.prefix else ""
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kwargs)
            keys += [item["Key"] for item in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                numbered = [
                    k
                    for k in keys
                    if k.endswith(".json")
                    and "/" not in k[len(prefix) :]
                    and k[len(prefix) : -5].isdigit()
                ]
                return sorted(k for k in numbered if int(k[len(prefix) : -5]) > after)
            token = page["NextContinuationToken"]

    def _get(self, key: str) -> dict[str, Any]:
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return dict(json.loads(body))

    def read(self, after: int = 0) -> list[dict[str, Any]]:
        try:
            return [self._get(key) for key in self._keys(after)]
        except ValueError as exc:
            raise LedgerTampered("A ledger entry isn't valid JSON.") from exc
        except Exception as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc

    def last(self) -> dict[str, Any] | None:
        try:
            keys = self._keys()
            return self._get(keys[-1]) if keys else None
        except ValueError as exc:
            raise LedgerTampered("A ledger entry isn't valid JSON.") from exc
        except Exception as exc:
            raise LedgerUnavailable("The ledger can't be read.") from exc

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
def verify(
    entries: list[dict[str, Any]] | None = None, *, after: tuple[int, str] | None = None
) -> list[Entry]:
    """Every entry, checked: numbered 1, 2, 3, ..., each MAC right and each linked to the one
    before. LedgerTampered otherwise; LedgerUnavailable if it can't be read.

    `after=(seq, mac)`: only the entries after one already verified, checked to continue
    its chain (the gate's incremental check: the store is write-once, so a verified prefix
    stays verified; replay always checks the whole chain)."""
    found = store()
    if found is None:
        return []
    start, previous = after if after is not None else (0, "")
    raw = entries if entries is not None else found.read(start)
    checked: list[Entry] = []
    for expected, data in enumerate(raw, start=start + 1):
        entry = _entry(data)
        if entry.seq != expected:
            raise LedgerTampered(f"Ledger entry {expected} is missing or out of order.")
        if entry.prev != previous:
            raise LedgerTampered(f"Ledger entry {entry.seq} isn't linked to the one before.")
        if not hmac.compare_digest(entry.mac, _mac(entry.content())):
            raise LedgerTampered(f"Ledger entry {entry.seq} has been altered.")
        if entry.kind not in KINDS:
            raise LedgerTampered(f"Ledger entry {entry.seq} has an unknown kind.")
        checked.append(entry)
        previous = entry.mac
    return checked


def head() -> Entry | None:
    """The last entry (its MAC checked), or None for an empty ledger."""
    found = store()
    if found is None:
        return None
    data = found.last()
    if data is None:
        return None
    entry = _entry(data)
    if not hmac.compare_digest(entry.mac, _mac(entry.content())):
        raise LedgerTampered(f"Ledger entry {entry.seq} has been altered.")
    return entry


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
def append(kind: str, subject: UUID | str, *, at: datetime | None = None) -> Entry | None:
    """Record an erasure, inside the transaction that performs it (module docstring). None
    when the ledger is off. LedgerUnavailable propagates: the erasure doesn't happen."""
    if kind not in KINDS:
        raise ValueError(f"Unknown ledger kind {kind!r}.")
    found = store()
    if found is None:
        return None
    if not connection.in_atomic_block:
        raise RuntimeError("Ledger entries are appended inside the erasing transaction.")
    lock()
    moment = (at or timezone.now()).isoformat()
    for _ in range(_APPEND_ATTEMPTS):
        last = head()
        draft = Entry(
            seq=(last.seq if last else 0) + 1,
            kind=kind,
            subject=str(subject),
            at=moment,
            prev=last.mac if last else "",
            mac="",
        )
        entry = Entry(**{**draft.content(), "mac": _mac(draft.content())})
        if found.create(entry.seq, entry.as_json()):
            applied_seq, _ = applied()
            # This database has applied everything up to the new entry, unless it was
            # already behind (a restore not yet replayed): then it stays behind.
            if last is None or applied_seq == last.seq:
                mark_applied(entry)
            return entry
    raise LedgerUnavailable("The ledger kept changing under this append; try again.")
