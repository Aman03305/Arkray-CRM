"""Removing a stored object for good, versions and all (final audit ARCH-4;
docs/privacy.md#access-requests).

`Storage.delete` is not enough for personal data in a versioned S3 bucket: it only adds a
delete marker, and every earlier version stays readable to anyone with the bucket's
credentials until a lifecycle rule expires it. `purge` removes every version and delete
marker of exactly one key (ListObjectVersions with the key as its prefix, then only entries
whose Key is that key: never `x.zip.bak` beside `x.zip`), in pages, and checks each
DeleteObjects answer's `Errors`: a partial failure is a failure, never success.

- **Retention**: S3 Object Lock (a retention period or a legal hold) refuses to delete a
  version with AccessDenied. That version is *retained*: reported (`PurgeResult.retained`),
  never retried in a loop here; the caller records it and tries again later.
- **Idempotent**: a key with nothing left under it purges to zero; a version already gone
  (NoSuchVersion, NoSuchKey) counts as removed.
- **Other stores**: a filesystem (or any store without versions) deletes the file; a missing
  file counts as removed. An S3-compatible store that doesn't implement ListObjectVersions
  falls back to a plain delete.

Failures to reach or use the store raise PurgeFailed with an error class from a fixed list
(logs and metrics; never a key or a bucket). `objects` lists a store's keys for a
reconciliation, a bounded page at a time; `overlaps` tells whether two stores share objects
(a reconciliation that deletes what no row knows must never run over another store's files).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from botocore import exceptions as boto
from django.core.files.storage import FileSystemStorage, Storage

# DeleteObjects takes at most 1,000 keys; ListObjectVersions returns at most 1,000 a page.
BATCH = 1000
# One key's versions: more pages than this is a runaway (a key rewritten in a loop), not an
# export; what is left is purged on the next attempt.
MAX_VERSION_PAGES = 50
# A reconciliation's listing: pages per call (each up to BATCH entries).
MAX_LIST_PAGES = 100
ERROR_CLASSES = ("access_denied", "missing_bucket", "server_error", "connection", "other")
_GONE = {"NoSuchVersion", "NoSuchKey"}
# What Object Lock answers for a version under retention or a legal hold (AWS and MinIO).
_RETAINED = {"AccessDenied"}
_UNSUPPORTED = {"NotImplemented", "XNotImplemented"}


class PurgeFailed(Exception):
    """The store could not be used to purge (unreachable, refused the listing, ...). Nothing
    is known to be removed: the caller retries later."""

    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


@dataclass(frozen=True, slots=True)
class PurgeResult:
    removed: int = 0  # objects, versions and delete markers removed
    retained: int = 0  # versions the store refused to delete (Object Lock retention, hold)
    failed: int = 0  # versions whose deletion failed otherwise (retry)
    versioned: bool = False  # versions were listed (S3), not a single object deleted

    @property
    def complete(self) -> bool:
        return self.retained == 0 and self.failed == 0

    def __add__(self, other: PurgeResult) -> PurgeResult:
        return PurgeResult(
            self.removed + other.removed,
            self.retained + other.retained,
            self.failed + other.failed,
            self.versioned or other.versioned,
        )


def is_s3(store: Storage) -> bool:
    return not isinstance(store, FileSystemStorage) and hasattr(store, "bucket_name")


def error_class(error: BaseException) -> str:
    if isinstance(error, boto.ClientError):
        response: dict[str, Any] = error.response
        status = int(response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
        code = str(response.get("Error", {}).get("Code", ""))
        if code == "NoSuchBucket":
            return "missing_bucket"
        if status == 403 or code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
            return "access_denied"
        if status >= 500 or code in {"SlowDown", "ServiceUnavailable", "InternalError"}:
            return "server_error"
        return "other"
    if isinstance(error, boto.NoCredentialsError | PermissionError):
        return "access_denied"
    if isinstance(error, boto.BotoCoreError | ConnectionError | TimeoutError):
        return "connection"
    return "other"


def _code(error: boto.ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def purge(store: Storage, key: str) -> PurgeResult:
    """Remove the object under `key` (relative to the store's location) and, in S3, every
    version and delete marker of it. Raises PurgeFailed when the store can't be used."""
    if not is_s3(store):
        try:
            existed = store.exists(key)
            store.delete(key)
        except FileNotFoundError:
            return PurgeResult()
        except OSError as exc:
            raise PurgeFailed(error_class(exc)) from exc
        return PurgeResult(removed=int(existed))
    try:
        return _purge_versions(store, key)
    except boto.ClientError as exc:
        if _code(exc) in _UNSUPPORTED:  # no versions on this store: one object to delete
            return _plain_delete(store, key)
        raise PurgeFailed(error_class(exc)) from exc
    except (boto.BotoCoreError, OSError) as exc:
        raise PurgeFailed(error_class(exc)) from exc


def _plain_delete(store: Storage, key: str) -> PurgeResult:
    try:
        store.delete(key)  # django-storages: a missing object is not an error
    except (boto.ClientError, boto.BotoCoreError, OSError) as exc:
        raise PurgeFailed(error_class(exc)) from exc
    return PurgeResult(removed=1)


def _purge_versions(store: Any, key: str) -> PurgeResult:
    client = store.connection.meta.client
    bucket = store.bucket_name
    full_key = str(store._normalize_name(key))  # the backend's own mapping (its location)
    result = PurgeResult(versioned=True)
    markers: dict[str, str] = {}
    for _page in range(MAX_VERSION_PAGES):
        listing = client.list_object_versions(Bucket=bucket, Prefix=full_key, **markers)
        found = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for kind in ("Versions", "DeleteMarkers")
            for item in listing.get(kind, [])
            if item["Key"] == full_key  # exactly this key, never one that starts with it
        ]
        for start in range(0, len(found), BATCH):
            result += _delete_batch(client, bucket, found[start : start + BATCH])
        if not listing.get("IsTruncated"):
            return result
        markers = {
            "KeyMarker": listing.get("NextKeyMarker", ""),
            "VersionIdMarker": listing.get("NextVersionIdMarker", ""),
        }
    return result + PurgeResult(failed=1)  # more pages than an export ever has: next time


def _delete_batch(client: Any, bucket: str, objects: list[dict[str, str]]) -> PurgeResult:
    """One DeleteObjects call. Quiet: the answer lists only the versions it did NOT delete
    (`Errors`), each of which is counted; one already gone counts as removed."""
    response = client.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
    codes = [str(error.get("Code", "")) for error in response.get("Errors", [])]
    retained = sum(code in _RETAINED for code in codes)
    failed = sum(code not in _RETAINED and code not in _GONE for code in codes)
    return PurgeResult(removed=len(objects) - retained - failed, retained=retained, failed=failed)


# --- listing (reconciliation) ----------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StoredKey:
    key: str  # relative to the store's location
    modified: datetime  # the newest version's (S3) or the file's modification time


def objects(store: Storage, *, after: str, limit: int) -> tuple[list[StoredKey], bool]:
    """Up to `limit` keys of the store sorting after `after`, in key order, and whether the
    listing reached the end. In S3 every key that has any version or delete marker (a key
    whose current version was deleted still holds its earlier ones). Raises PurgeFailed."""
    try:
        if is_s3(store):
            return _s3_keys(store, after, limit)
        return _file_keys(Path(str(store.location)), after, limit)  # type: ignore[attr-defined]
    except (boto.ClientError, boto.BotoCoreError, OSError) as exc:
        raise PurgeFailed(error_class(exc)) from exc


def _s3_keys(store: Any, after: str, limit: int) -> tuple[list[StoredKey], bool]:
    client = store.connection.meta.client
    location = str(store.location).strip("/")
    prefix = f"{location}/" if location else ""
    newest: dict[str, datetime] = {}
    markers: dict[str, str] = {"KeyMarker": prefix + after} if after else {}
    done = False
    for _page in range(MAX_LIST_PAGES):
        listing = client.list_object_versions(
            Bucket=store.bucket_name, Prefix=prefix, MaxKeys=BATCH, **markers
        )
        for kind in ("Versions", "DeleteMarkers"):
            for item in listing.get(kind, []):
                key = str(item["Key"])[len(prefix) :]
                modified = item["LastModified"]
                if key not in newest or modified > newest[key]:
                    newest[key] = modified
        done = not listing.get("IsTruncated")
        if done or len(newest) > limit:
            break
        markers = {
            "KeyMarker": listing.get("NextKeyMarker", ""),
            "VersionIdMarker": listing.get("NextVersionIdMarker", ""),
        }
    keys = sorted(newest)
    if len(keys) > limit:
        keys, done = keys[:limit], False
    return [StoredKey(key, newest[key]) for key in keys], done


def _file_keys(root: Path, after: str, limit: int) -> tuple[list[StoredKey], bool]:
    found: list[StoredKey] = []
    for path in _walk(root, ""):
        if path <= after:
            continue
        if len(found) == limit:
            return found, False
        stat = (root / path).stat()
        found.append(StoredKey(path, datetime.fromtimestamp(stat.st_mtime, tz=UTC)))
    return found, True


def _walk(directory: Path, prefix: str) -> Iterator[str]:
    """Relative paths of the files under `directory`, in code-point order (a directory sorts
    as its name plus "/", as S3 keys do)."""
    try:
        with os.scandir(directory) as entries:
            listed = sorted(
                (
                    (entry.name + "/" if entry.is_dir(follow_symlinks=False) else entry.name, entry)
                    for entry in entries
                ),
                key=lambda pair: pair[0],
            )
    except FileNotFoundError:
        return  # nothing stored yet
    for name, entry in listed:
        if name.endswith("/"):
            yield from _walk(Path(entry.path), prefix + name)
        elif entry.is_file(follow_symlinks=False):
            yield prefix + name


def overlaps(store: Storage, other: Storage) -> bool:
    """Whether two stores can hold the same objects: the same bucket with one location inside
    the other (or either at the bucket's root), the same directory or one inside the other,
    or kinds this can't compare (assumed to overlap: the safe answer)."""
    if is_s3(store) and is_s3(other):
        if store.bucket_name != other.bucket_name:  # type: ignore[attr-defined]
            return False
        a = str(store.location).strip("/")  # type: ignore[attr-defined]
        b = str(other.location).strip("/")  # type: ignore[attr-defined]
        return not a or not b or (a + "/").startswith(b + "/") or (b + "/").startswith(a + "/")
    if isinstance(store, FileSystemStorage) and isinstance(other, FileSystemStorage):
        a_path = Path(store.location).resolve()
        b_path = Path(other.location).resolve()
        return a_path == b_path or a_path in b_path.parents or b_path in a_path.parents
    return is_s3(store) == is_s3(other)  # one of each kind never shares an object
