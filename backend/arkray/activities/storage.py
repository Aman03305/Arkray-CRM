"""Attachment files: what is accepted, how it is received, where it is kept, how it is
scanned (docs/activities.md#attachments).

- **Types**: an explicit catalog of business documents and images the server can recognise
  by their *content*; ATTACHMENT_ALLOWED_EXTENSIONS enables a subset (a system check refuses
  anything outside the catalog). Executables, scripts, HTML, SVG, archives and everything
  else are refused, whatever their name or declared type. A file must be what its extension
  says: a PDF starts like a PDF, an image is that image format, Office files are the right
  OOXML package (without macros, ActiveX, OLE objects or anything fetched from elsewhere when
  opened: its XML parsed, never searched as bytes), text is UTF-8 without NUL bytes.
- **Names**: the uploaded name is display text only. It is cleaned (no path, no control or
  bidi characters, bounded) and never used for storage: objects live under generated keys.
- **Receiving**: the body is read from the request stream in chunks, hashed (SHA-256) and
  counted as it arrives; more than ATTACHMENT_MAX_BYTES stops the read at once. Spooled to a
  temporary file above ATTACHMENT_SPOOL_MEMORY_BYTES, never held whole in memory.
- **Storage**: STORAGES["attachments"] (a private directory locally, private S3-compatible
  storage in production). Failures surface as StorageUnavailable (ObjectMissing when the
  store answered that the object is gone): the upload or download fails with a 503 (a 410
  for a lost file) and nothing else in the CRM depends on storage. Every call is guarded
  (docs/reliability.md#attachment-storage): a wall-clock deadline, a per-process cap on
  calls in flight, and a circuit breaker that fails fast during an outage.
- **Scanning**: none, or a ClamAV daemon (clamd's INSTREAM protocol) when ATTACHMENT_SCANNER
  is set; never a pretend scanner. Only clamd's exact clean reply is "clean"; anything else
  (no answer, a timeout, a dropped connection, an error reply such as a stream over clamd's
  StreamMaxLength, a reply of any other shape, or bytes that aren't the uploaded file) is
  no verdict, and the file stays unavailable.
"""

from __future__ import annotations

import codecs
import contextlib
import errno
import hashlib
import itertools
import logging
import math
import os
import re
import socket
import struct
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import IO, Any, Protocol, cast
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree
from botocore import exceptions as _boto
from defusedxml import DefusedXmlException
from django.conf import settings
from django.core.cache import cache
from django.core.checks import Error, register
from django.core.files import File
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage, storages

from arkray.core import storage_purge
from arkray.core.redis import CircuitBreaker
from arkray.core.text import TextRejected, clean_line

from . import telemetry

logger = logging.getLogger(__name__)

CHUNK_BYTES = 64 * 1024
HEAD_BYTES = 8 * 1024
MAX_ZIP_ENTRIES = 10_000
LARGEST_FILE_BYTES = 100 * 1024 * 1024  # the activities_attachment_size CHECK
# Office packages: what may be embedded (documents and pictures; never OLE objects, ActiveX
# controls or macro projects, whatever their part is called), and the small XML parts read
# to find active content declared by type or relationship (bounded: a zip bomb stays cheap).
SAFE_EMBEDDINGS = (".xlsx", ".docx", ".pptx", ".png", ".jpeg", ".jpg", ".gif", ".emf", ".wmf")
# Declared content types (and part names in [Content_Types].xml) that mean active content or
# something fetched from elsewhere when the file opens: macros, ActiveX, OLE objects, a
# workbook's links to other workbooks and its data connections, documents Word imports when it
# opens the file (an altChunk's RTF, which can carry OLE objects and self-updating links, or
# MHT/HTML, which can fetch remote pictures), Office add-ins (deep security audit review).
ACTIVE_CONTENT_TYPES = (
    "vbaproject",
    "macroenabled",
    "activex",
    "oleobject",
    "vbadata",
    "externallink",
    "spreadsheetml.connections",
    "application/rtf",
    "text/rtf",
    "message/rfc822",
    "text/html",
    "xhtml",
    "webextension",
    "taskpane",
)
# Part names, by their ending, of documents Word would import (an altChunk's RTF, MHT, HTML).
IMPORTED_DOCUMENTS = (".rtf", ".mht", ".mhtml", ".htm", ".html", ".xhtml")
# Relationship types (anywhere in the Type URI, case ignored) refused whatever their target
# mode: OLE objects (CVE-2017-0199, "Follina"), remote templates, ActiveX, macro projects,
# master-document subdocuments, frames, links to other workbooks, data connections, imported
# documents (aFChunk: Word's altChunk), Office add-ins (webextension, webextensionTaskpanes:
# an add-in loads from its web address when the file opens).
REFUSED_RELATIONSHIPS = (
    "oleobject",
    "attachedtemplate",
    "activex",
    "vbaproject",
    "vbadata",
    "subdocument",
    "frame",
    "externallink",
    "connections",
    "afchunk",
    "webextension",
    "taskpane",
)
# By the Type URI's last segment: an ActiveX control.
REFUSED_RELATIONSHIP_NAMES = frozenset({"control"})
# The one relationship that may point outside the package: a hyperlink, which Office only
# follows when someone clicks it (with a prompt). Anything else external is fetched on open.
# Its exact Type URI (Transitional or Strict), case ignored: never any URI that merely ends in
# "/hyperlink" (deep security audit review).
EXTERNAL_ALLOWED = frozenset(
    {
        "http://schemas.openxmlformats.org/officedocument/2006/relationships/hyperlink",
        "http://purl.oclc.org/ooxml/officedocument/relationships/hyperlink",
    }
)
# A relationship target that names another place (a URL, a UNC path) rather than a part.
_ELSEWHERE = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|[\\/]{2})", re.IGNORECASE)
_XML_DECLARATION = re.compile(r"<\?xml[^>]{0,200}?\?>")
_DECLARED_ENCODING = re.compile(r"""encoding\s*=\s*(["'])(?P<name>[^"']{0,40})\1""")
OOXML_XML_PART_BYTES = 1024 * 1024
OOXML_XML_TOTAL_BYTES = 4 * 1024 * 1024


class StorageUnavailable(Exception):
    """The attachment store could not be reached or refused the operation. `error_class`
    says why, from a fixed list (telemetry.ERROR_CLASSES): logs and metrics, never users."""

    def __init__(self, *args: object, error_class: str = "other") -> None:
        super().__init__(*args)
        self.error_class = error_class


class ObjectMissing(StorageUnavailable):
    """The store answered, and there is no object under this key (404, NoSuchKey): a file
    that is gone, not an outage. A StorageUnavailable, so code that only knows "storage
    failed" stays safe; callers that can tell the user the truth catch it first."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args, error_class="missing")


class ScannerUnavailable(Exception):
    """The malware scanner gave no verdict (the job retries, then goes dead): never "clean".
    `reason` says why, from SCAN_FAILURES: logs and the outbox's last error, never users."""

    def __init__(self, reason: str = "protocol") -> None:
        super().__init__(f"No verdict from the scanner ({reason}).")
        self.reason = reason


class RejectedFile(ValueError):
    """The file can't be accepted; the message is for the person who uploaded it."""


# --- the catalog ----------------------------------------------------------------------------------
def _starts(*prefixes: bytes) -> Callable[[bytes, IO[bytes]], bool]:
    return lambda head, _file: any(head.startswith(prefix) for prefix in prefixes)


def _webp(head: bytes, _file: IO[bytes]) -> bool:
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _ooxml(main_part: str) -> Callable[[bytes, IO[bytes]], bool]:
    """An Office Open XML package with its main part and no active content: no macro project,
    OLE object or ActiveX control (by part name, embedded file type, declared content type,
    relationship type), nothing fetched from elsewhere when it opens (a remote template, a
    linked picture or object, a link to another workbook, a data connection). The zip's
    central directory, then only its small XML parts ([Content_Types].xml, relationships;
    each and in total bounded) are read, so a zip bomb costs little here (enhancement security
    review: a renamed VBA part, an OLE package wrapping an executable, ActiveX all passed a
    name-only check). Anything that can't be read or parsed is refused (fail closed)."""

    def check(head: bytes, file: IO[bytes]) -> bool:
        if not head.startswith(b"PK\x03\x04"):
            return False
        file.seek(0)
        try:
            with zipfile.ZipFile(file) as package:
                infos = package.infolist()
                if len(infos) > MAX_ZIP_ENTRIES:
                    return False
                lowered = {info.filename.lower() for info in infos}
                if "[content_types].xml" not in lowered or main_part not in lowered:
                    return False
                return not _active_content(package, infos)
        except Exception:  # noqa: BLE001 — a corrupt stream (zlib.error, ...), an encrypted
            return False  # entry, an unsupported compression: not a package we can vouch for

    return check


def _active_content(package: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> bool:
    """Whether the package holds, declares or links to active or external content. The XML
    is parsed, never searched as bytes (deep security audit M-3: single quotes, spaces around
    "=", a character reference such as attached&#84;emplate, or a UTF-16 part all got past a
    byte search)."""
    budget = OOXML_XML_TOTAL_BYTES
    for info in infos:
        # Office matches part names ignoring case; a percent-encoded name is matched decoded.
        name = unquote(info.filename).lower().lstrip("/")
        if name.endswith(".bin") and "/printersettings/" not in name:
            return True  # vbaProject.bin (under any name), oleObject*.bin, activeX*.bin
        if "/activex/" in name or "/externallinks/" in name or "/webextensions/" in name:
            return True
        if name.endswith(IMPORTED_DOCUMENTS):
            return True  # an altChunk's RTF, MHT or HTML, imported when Word opens the file
        if "/embeddings/" in name and not name.endswith(SAFE_EMBEDDINGS):
            return True
        declares = name == "[content_types].xml"
        if declares or name.endswith(".rels"):
            if info.file_size > OOXML_XML_PART_BYTES or info.file_size > budget:
                return True  # no real package has XML parts this large: refuse, unread
            with package.open(info) as part:
                raw = part.read(OOXML_XML_PART_BYTES + 1)  # bounded whatever the header says
            budget -= len(raw)
            if len(raw) > OOXML_XML_PART_BYTES or budget < 0:
                return True
            root = _parse_part(raw)
            if root is None:
                return True  # not well-formed, a DTD or entity, or an encoding not allowed
            if _declares_active(root) if declares else _links_active(root):
                return True
    return False


def _parse_part(raw: bytes) -> Element | None:
    """The part's XML, or None. OPC parts are UTF-8 or UTF-16 (ECMA-376 Part 2, M1.17): the
    bytes are decoded here (UTF-16 needs its byte-order mark), a declared encoding must agree,
    and the parser is given the decoded text, so what is checked is exactly what Office
    reads. No DTD, entity or external reference is allowed (defusedxml)."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding, allowed = "utf-16", {"utf-16"}
    else:
        encoding, allowed = "utf-8", {"utf-8"}
        raw = raw.removeprefix(codecs.BOM_UTF8)
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        # Never XML, and it would let the parser guess another encoding: expat reads bytes
        # starting "<\x00" as UTF-16, whatever it was told.
        return None
    if text.startswith("<?xml"):
        declaration = _XML_DECLARATION.match(text)
        if declaration is None:
            return None
        if "encoding" in declaration[0]:
            declared = _DECLARED_ENCODING.search(declaration[0])
            if declared is None or declared["name"].strip().casefold() not in allowed:
                return None
    try:
        return defusedxml.ElementTree.fromstring(text, forbid_dtd=True, forbid_entities=True)
    except (ParseError, DefusedXmlException, ValueError):
        return None


def _local(name: str) -> str:
    """An element's or attribute's name without its namespace, case ignored."""
    return name.rpartition("}")[2].casefold()


def _declares_active(root: Element) -> bool:
    """[Content_Types].xml: any declared type (or part name) that is active content."""
    for element in root.iter():
        for value in (*element.attrib.values(), element.text or ""):
            folded = value.casefold()
            if any(kind in folded for kind in ACTIVE_CONTENT_TYPES):
                return True
    return False


def _links_active(root: Element) -> bool:
    """A relationships part: any relationship of a refused type, or pointing outside the
    package (TargetMode not "Internal", or a target that is a URL or a UNC path) unless it is
    a hyperlink. Every element and attribute is looked at, namespaced or not, so a variant
    Office might honour can't slip past by its spelling."""
    for element in root.iter():
        types: list[str] = []
        modes: list[str] = []
        targets: list[str] = []
        for key, value in element.attrib.items():
            found = {"type": types, "targetmode": modes, "target": targets}.get(_local(key))
            if found is not None:
                found.append(value.strip().casefold())
        if not (types or modes or targets):
            continue
        if any(word in kind for kind in types for word in REFUSED_RELATIONSHIPS):
            return True
        names = {kind.rstrip("/").rpartition("/")[2] for kind in types}
        if names & REFUSED_RELATIONSHIP_NAMES:
            return True
        if types and set(types) <= EXTERNAL_ALLOWED:
            continue  # a hyperlink, wherever it points
        if any(mode != "internal" for mode in modes):
            return True  # "External" (an image, a video, an object or a data source)
        if any(_ELSEWHERE.match(target) for target in targets):
            return True
    return False


def _utf8_text(head: bytes, file: IO[bytes]) -> bool:
    """UTF-8 (a BOM allowed) without NUL bytes, checked across the whole file."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    file.seek(0)
    for chunk in iter(lambda: file.read(CHUNK_BYTES), b""):
        if b"\x00" in chunk:
            return False
        try:
            decoder.decode(chunk)
        except UnicodeDecodeError:
            return False
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class FileKind:
    content_type: str
    recognise: Callable[[bytes, IO[bytes]], bool]
    # Shown inline (an <img> on the note) once validated; everything else only downloads.
    previewable: bool = False


CATALOG: dict[str, FileKind] = {
    "pdf": FileKind("application/pdf", _starts(b"%PDF-")),
    "png": FileKind("image/png", _starts(b"\x89PNG\r\n\x1a\n"), previewable=True),
    "jpg": FileKind("image/jpeg", _starts(b"\xff\xd8\xff"), previewable=True),
    "jpeg": FileKind("image/jpeg", _starts(b"\xff\xd8\xff"), previewable=True),
    "webp": FileKind("image/webp", _webp, previewable=True),
    "gif": FileKind("image/gif", _starts(b"GIF87a", b"GIF89a"), previewable=True),
    "docx": FileKind(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        _ooxml("word/document.xml"),
    ),
    "xlsx": FileKind(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _ooxml("xl/workbook.xml"),
    ),
    "pptx": FileKind(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        _ooxml("ppt/presentation.xml"),
    ),
    "csv": FileKind("text/csv", _utf8_text),
    "txt": FileKind("text/plain", _utf8_text),
}


def allowed_extensions() -> list[str]:
    return [ext.lower() for ext in settings.ATTACHMENT_ALLOWED_EXTENSIONS]


@register()
def check_allowed_extensions(**_: Any) -> list[Error]:
    unknown = sorted(set(allowed_extensions()) - set(CATALOG))
    if unknown:
        return [
            Error(
                f"ATTACHMENT_ALLOWED_EXTENSIONS lists types the server can't recognise: "
                f"{', '.join(unknown)}. Choose from {', '.join(sorted(CATALOG))}.",
                id="arkray.E020",
            )
        ]
    scanner = settings.ATTACHMENT_SCANNER
    if scanner and _scanner_address(scanner) is None:
        return [Error("ATTACHMENT_SCANNER must be empty or clamd://host:port.", id="arkray.E021")]
    if not 0 < settings.ATTACHMENT_MAX_BYTES <= LARGEST_FILE_BYTES:
        # The database's CHECK (activities_attachment_size) bounds every stored size.
        return [
            Error(
                f"ATTACHMENT_MAX_BYTES must be between 1 and {LARGEST_FILE_BYTES} (100 MB).",
                id="arkray.E022",
            )
        ]
    if settings.ATTACHMENT_MAX_BYTES_PER_USER < 0:
        return [
            Error(
                "ATTACHMENT_MAX_BYTES_PER_USER must be 0 (no quota) or a number of bytes.",
                id="arkray.E023",
            )
        ]
    return []


# --- names ----------------------------------------------------------------------------------------
NAME_MAX_LENGTH = 200


def clean_filename(raw: str) -> tuple[str, str]:
    """The display name and lower-case extension of an uploaded file's name (sent
    percent-encoded). Paths are dropped (only the last component counts); control, bidi and
    other invisible characters are refused; the extension must be allowed."""
    try:
        name = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        raise RejectedFile("The file name isn't valid.") from None
    name = unicodedata.normalize("NFC", name)
    name = re.split(r"[\\/]", name)[-1]
    try:
        name = clean_line(name)
    except TextRejected:
        raise RejectedFile(
            "Remove the invisible or control characters from the file name."
        ) from None
    name = name.lstrip(". ")
    if not name:
        raise RejectedFile("The file needs a name.")
    if len(name) > NAME_MAX_LENGTH:
        raise RejectedFile(f"Use a file name of at most {NAME_MAX_LENGTH} characters.")
    stem, dot, extension = name.rpartition(".")
    extension = extension.lower()
    if not dot or not stem or extension not in allowed_extensions():
        allowed = ", ".join(sorted({e.upper() for e in allowed_extensions()}))
        raise RejectedFile(f"This file type isn't allowed. Allowed: {allowed}.")
    return name, extension


def ascii_fallback(name: str) -> str:
    """A plain-ASCII version of a file name for the Content-Disposition `filename=` (the
    exact name goes in `filename*`)."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", folded).strip(" .") or "file"
    return safe[:NAME_MAX_LENGTH]


# --- receiving ------------------------------------------------------------------------------------
@dataclass(slots=True)
class Received:
    file: IO[bytes]  # a spooled temporary file
    size: int
    sha256: str
    head: bytes


class Readable(Protocol):
    def read(self, size: int = ..., /) -> bytes: ...


def receive(stream: Readable, *, max_bytes: int) -> Received:
    """Read an upload from the request stream, bounded: RejectedFile as soon as it exceeds
    `max_bytes`, or when it is empty."""
    # Not a context manager: the spool outlives this function (the caller closes it).
    spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115
        max_size=settings.ATTACHMENT_SPOOL_MEMORY_BYTES
    )
    digest = hashlib.sha256()
    size = 0
    head = b""
    try:
        while True:
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise RejectedFile(f"Files can be at most {max_bytes // (1024 * 1024)} MB.")
            if len(head) < HEAD_BYTES:
                head += chunk[: HEAD_BYTES - len(head)]
            digest.update(chunk)
            spool.write(chunk)
    except RejectedFile:
        spool.close()
        raise
    if size == 0:
        spool.close()
        raise RejectedFile("This file is empty.")
    spool.seek(0)
    return Received(file=spool, size=size, sha256=digest.hexdigest(), head=head)


def recognise(extension: str, received: Received) -> FileKind:
    """The catalog entry, if the content is what the extension says (RejectedFile else)."""
    kind = CATALOG[extension]
    try:
        matches = kind.recognise(received.head, received.file)
    finally:
        received.file.seek(0)
    if not matches:
        raise RejectedFile(
            f"This file's content doesn't match its type (.{extension}). Upload the original file."
        )
    return kind


# --- storage --------------------------------------------------------------------------------------
# Every call to the store goes through _call() (docs/reliability.md#attachment-storage):
# - a wall-clock deadline (ATTACHMENT_STORAGE_DEADLINE_S): the call runs on a small pool and
#   the request stops waiting when it passes, whatever botocore's retries and per-read
#   timeouts would add up to (a stalled body, a slow drip of bytes). The abandoned call is
#   told to stop at its next chunk and ends within botocore's own timeouts;
# - a cap on calls in flight per process (ATTACHMENT_STORAGE_MAX_IN_FLIGHT, abandoned ones
#   included): beyond it a call fails at once, so storage trouble can't take every thread;
# - a circuit breaker: after ATTACHMENT_STORAGE_BREAKER_FAILURES outage-like failures in a
#   row, calls fail at once (no network call) for a cool-down that doubles while the outage
#   lasts; whether the store is back is checked on a background thread, never in a request.
#   A missing object (404) or a refusal (403, a configuration fault answered at once) is an
#   answer, not an outage: neither trips it.
#
# The breaker and a second cap are also shared by every process through the cache (Redis; the
# AI breaker's pattern, arkray/ai/breaker.py): BREAKER_FAILURES outage-like failures anywhere
# in the deployment within a cool-down open the breaker for every process, and at most
# ATTACHMENT_STORAGE_MAX_IN_FLIGHT_SHARED calls are in flight at once across all of them. A
# per-process breaker alone needed three deadlines in EACH gunicorn process before it opened:
# in the capacity test, 8 people uploading through a black-holed store held all 8 web workers
# and every other page took 9-11 s (docs/capacity.md#attachment-storage-failures). Without
# Redis each process keeps its own breaker and cap, as before.
_SHARED_OPEN = "attachments:storage:open"
_SHARED_FAILURES = "attachments:storage:failures"
_SHARED_IN_FLIGHT = "attachments:storage:in_flight"
PROBE_PREFIX = ".arkray-probe-"
PROBE_CACHE_S = 30.0
STORAGE_RETRY_AFTER_S = 30
_OUTAGES = frozenset({"timeout", "connection", "server_error", "no_space", "integrity", "other"})


_NO_SPACE = frozenset(
    code for code in (getattr(errno, "ENOSPC", None), getattr(errno, "EDQUOT", None)) if code
)


class _Integrity(Exception):
    """The store accepted a write or a read that isn't the whole file."""


class _Cancelled(Exception):
    """The caller stopped waiting (the deadline passed): stop moving bytes."""


def _store() -> Storage:
    return storages["attachments"]


def backend_name(store: Storage | None = None) -> str:
    """ "filesystem" or "s3": a metric label and a log field (never the bucket or path)."""
    store = store if store is not None else _store()
    if isinstance(store, FileSystemStorage):
        return "filesystem"
    return "s3" if hasattr(store, "bucket_name") else "other"


def _errors_of(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def classify(error: BaseException) -> str:
    """Why a storage call failed, as one of telemetry.ERROR_CLASSES (the exception and what
    it wraps: s3transfer and Django re-raise what botocore raised)."""
    for found in _errors_of(error):
        error_class = _classify_one(found)
        if error_class is not None:
            return error_class
    return "other"


def _classify_one(error: BaseException) -> str | None:
    if isinstance(error, StorageUnavailable):
        return error.error_class
    if isinstance(error, _Integrity):
        return "integrity"
    if isinstance(error, FileNotFoundError):
        return "missing"
    if isinstance(error, PermissionError):
        return "access_denied"
    if isinstance(error, OSError) and error.errno in _NO_SPACE:
        return "no_space"
    if isinstance(error, _boto.ClientError):
        response: dict[str, Any] = error.response
        status = int(response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0)
        code = str(response.get("Error", {}).get("Code", ""))
        if status == 404 or code in {"NoSuchKey", "NotFound", "404"}:
            return "missing"
        if status == 403 or code in {"AccessDenied", "InvalidAccessKeyId", "403"}:
            return "access_denied"
        if status == 507 or code in {"QuotaExceeded", "XMinioStorageFull"}:
            return "no_space"
        if status >= 500 or code in {"SlowDown", "ServiceUnavailable", "InternalError"}:
            return "server_error"
        return "other"
    if isinstance(error, _boto.ConnectTimeoutError | _boto.ReadTimeoutError):
        return "timeout"
    if isinstance(error, _boto.NoCredentialsError):
        return "access_denied"
    if isinstance(error, _boto.ConnectionError | _boto.HTTPClientError):
        return "connection"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ConnectionError):
        return "connection"
    return None


@dataclass
class _Guard:
    config: tuple[float, int, float, int]
    breaker: CircuitBreaker
    slots: threading.BoundedSemaphore
    pool: ThreadPoolExecutor
    failures: int = 0  # outage-like failures in a row
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def deadline(self) -> float:
        return self.config[0]

    @property
    def threshold(self) -> int:
        return self.config[1]


_guard_lock = threading.Lock()
_current: _Guard | None = None


def _guard() -> _Guard:
    """This process's guard, built from the settings on first use (and again if they
    change: tests override them)."""
    global _current
    config = (
        float(settings.ATTACHMENT_STORAGE_DEADLINE_S),
        int(settings.ATTACHMENT_STORAGE_BREAKER_FAILURES),
        float(settings.ATTACHMENT_STORAGE_BREAKER_COOLDOWN_S),
        int(settings.ATTACHMENT_STORAGE_MAX_IN_FLIGHT),
    )
    guard = _current
    if guard is not None and guard.config == config:
        return guard
    with _guard_lock:
        if _current is None or _current.config != config:
            if _current is not None:
                _current.pool.shutdown(wait=False)
            _, _, cooldown, in_flight = config
            _current = _Guard(
                config=config,
                breaker=CircuitBreaker(
                    cooldown, cooldown * 8, event="attachment_storage_circuit_opened"
                ),
                slots=threading.BoundedSemaphore(in_flight),
                pool=ThreadPoolExecutor(
                    max_workers=in_flight, thread_name_prefix="attachment-storage"
                ),
            )
        return _current


def reset_guard() -> None:
    """Forget the breaker's state and the calls in flight (tests; a new process starts so)."""
    global _current
    with _guard_lock:
        if _current is not None:
            _current.pool.shutdown(wait=False)
        _current = None
    with contextlib.suppress(Exception):  # the cache is optional
        cache.delete_many([_SHARED_OPEN, _SHARED_FAILURES, _SHARED_IN_FLIGHT])


def breaker_open() -> bool:
    guard = _current
    return (
        guard is not None and (guard.breaker.is_open() or guard.breaker.recovering())
    ) or _shared_open()


def _shared_open() -> bool:
    try:
        return bool(cache.get(_SHARED_OPEN))
    except Exception:  # noqa: BLE001 — the cache is optional
        return False


def _shared_acquire() -> bool | None:
    """A slot under the deployment-wide cap: True, False (the cap is reached), or None when
    the cache can't tell (Redis down): then only this process's cap applies."""
    ttl = max(int(settings.ATTACHMENT_STORAGE_DEADLINE_S) * 2, 30)  # a leaked slot expires
    try:
        cache.add(_SHARED_IN_FLIGHT, 0, timeout=ttl)
        count: int | None = cache.incr(_SHARED_IN_FLIGHT)
    except Exception:  # noqa: BLE001 — the cache is optional (and incr on a vanished key)
        return None
    if count is None:  # django-redis with IGNORE_EXCEPTIONS: Redis didn't answer
        return None
    if count > int(settings.ATTACHMENT_STORAGE_MAX_IN_FLIGHT_SHARED):
        _shared_release()
        return False
    return True


def _shared_release() -> None:
    with contextlib.suppress(Exception):  # the cache is optional
        cache.decr(_SHARED_IN_FLIGHT)


def _answered(guard: _Guard) -> None:
    if guard.failures:
        with guard.lock:
            guard.failures = 0
    guard.breaker.succeeded()
    with contextlib.suppress(Exception):  # the store answered: the outage count starts again
        cache.delete(_SHARED_FAILURES)


def _failed(guard: _Guard) -> None:
    with guard.lock:
        guard.failures += 1
        trip = guard.failures >= guard.threshold
        if trip:
            guard.failures = 0  # after the cool-down, the count starts again
    if trip:
        guard.breaker.trip()
    cooldown = float(settings.ATTACHMENT_STORAGE_BREAKER_COOLDOWN_S)
    try:
        cache.add(_SHARED_FAILURES, 0, timeout=max(cooldown, 1.0))
        failures = cache.incr(_SHARED_FAILURES)
        if failures is not None and failures >= guard.threshold:
            cache.set(_SHARED_OPEN, 1, timeout=max(cooldown, 1.0))
            cache.delete(_SHARED_FAILURES)
            logger.warning("attachment_storage_shared_circuit_opened", extra={"failures": failures})
    except Exception:  # noqa: BLE001,S110 — the cache is optional; this process's breaker stands
        pass


def _call[T](operation: str, work: Callable[[threading.Event], T]) -> T:
    """`work(cancel)` on the guard's pool, within the deadline. StorageUnavailable (with
    its error class) if it fails, ObjectMissing if the store says the object is gone."""
    guard = _guard()
    if guard.breaker.is_open() or guard.breaker.recovering():
        if not guard.breaker.is_open():
            guard.breaker.probe(_probe)
        telemetry.storage_call(operation, 0.0, "circuit_open")
        raise StorageUnavailable("The circuit is open.", error_class="circuit_open")
    if _shared_open():  # another process saw the outage
        telemetry.storage_call(operation, 0.0, "circuit_open")
        raise StorageUnavailable("The circuit is open.", error_class="circuit_open")
    if not guard.slots.acquire(blocking=False):
        telemetry.storage_call(operation, 0.0, "busy")
        raise StorageUnavailable("Too many storage calls in flight.", error_class="busy")
    shared = _shared_acquire()
    if shared is False:
        guard.slots.release()
        telemetry.storage_call(operation, 0.0, "busy")
        raise StorageUnavailable("Too many storage calls in flight.", error_class="busy")

    cancel = threading.Event()

    def release() -> None:
        guard.slots.release()
        if shared:
            _shared_release()

    def run() -> T:
        try:
            return work(cancel)
        finally:
            release()

    started = time.monotonic()
    try:
        future = guard.pool.submit(run)
    except RuntimeError as error:  # the pool is shutting down with the process
        release()
        raise StorageUnavailable(error_class="other") from error
    failure: BaseException
    try:
        result = future.result(timeout=guard.deadline)
    except FutureTimeout as error:
        cancel.set()
        failure, error_class = error, "timeout"
    except Exception as error:  # noqa: BLE001 — classified below, then re-raised
        failure, error_class = error, classify(error)
    else:
        _answered(guard)
        telemetry.storage_call(operation, time.monotonic() - started, None)
        return result
    elapsed = time.monotonic() - started
    telemetry.storage_call(operation, elapsed, error_class)
    if error_class == "missing":
        _answered(guard)
        raise ObjectMissing() from failure
    if error_class in _OUTAGES:
        _failed(guard)
    else:
        _answered(guard)
    # The error's type and class only: its message can carry the bucket, endpoint or key.
    logger.warning(
        "attachment_storage_failed",
        extra={
            "operation": operation,
            "error_class": error_class,
            "error": type(failure).__name__,
            "backend": backend_name(),
            "duration_ms": round(elapsed * 1000),
        },
    )
    raise StorageUnavailable(error_class=error_class) from failure


class _Cancellable:
    """A source file that stops being readable once the caller gave up on the write."""

    def __init__(self, file: IO[bytes], cancel: threading.Event) -> None:
        self._file = file
        self._cancel = cancel

    def read(self, size: int = -1) -> bytes:
        if self._cancel.is_set():
            raise _Cancelled
        return self._file.read(size)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._file, name)


def save(key: str, file: IO[bytes]) -> None:
    """Store `file` under `key`. Returns only once the store holds the object with every
    byte: its size is read back from the store (a HEAD on S3, a stat on disk)."""
    file.seek(0, os.SEEK_END)
    expected = file.tell()
    file.seek(0)

    def work(cancel: threading.Event) -> None:
        store = _store()
        stored_as = store.save(key, File(cast(IO[bytes], _Cancellable(file, cancel))))
        if stored_as != key:  # the backend renamed it: never expected with generated keys
            store.delete(stored_as)
            raise _Integrity("The storage backend renamed the object.")
        if store.size(key) != expected:
            raise _Integrity("The stored object is not the whole file.")

    _call("save", work)


def open_file(key: str) -> IO[bytes]:
    """The object, readable at once. From S3 the whole object is fetched first, within the
    deadline, so a failure is an error response and never a download cut off after its 200
    (django-storages fetched it on the first read, while the response was streaming)."""

    def work(cancel: threading.Event) -> IO[bytes]:
        store = _store()
        if backend_name(store) == "s3":
            return _fetch(store, key, cancel)
        return store.open(key, "rb")

    return _call("open", work)


def _object_name(store: Any, key: str) -> str:
    """The bucket key django-storages uses for `key` (its location prefix joined)."""
    return str(store._normalize_name(key))  # the backend's own mapping


def _fetch(store: Any, key: str, cancel: threading.Event) -> IO[bytes]:
    response = store.connection.meta.client.get_object(
        Bucket=store.bucket_name, Key=_object_name(store, key)
    )
    body = response["Body"]
    # Not a context manager: the spool is the result (the caller closes it).
    spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115
        max_size=settings.ATTACHMENT_SPOOL_MEMORY_BYTES
    )
    try:
        received = 0
        for chunk in iter(lambda: body.read(CHUNK_BYTES), b""):
            if cancel.is_set():
                raise _Cancelled
            spool.write(chunk)
            received += len(chunk)
        if received != response.get("ContentLength", received):
            raise _Integrity("The object ended early.")
    except BaseException:
        spool.close()
        body.close()
        raise
    spool.seek(0)
    return spool


def delete(key: str) -> None:
    """Remove an object; one that is already gone counts as removed (idempotent)."""
    try:
        _call("delete", lambda _cancel: _store().delete(key))
    except ObjectMissing:
        return


def delete_versions(key: str) -> int:
    """S3 with versioning: remove every earlier version (and delete marker) of the object
    as well, so a deleted file doesn't live on until the bucket's lifecycle rule expires it
    (ATTACHMENT_S3_PURGE_VERSIONS; the credentials then need s3:ListBucketVersions and
    s3:DeleteObjectVersion). Returns how many versions were removed. Nothing to do for the
    filesystem store. Every page of versions, in batches DeleteObjects accepts, and a
    version it refused (its `Errors`: Object Lock retention, a missing permission) is a
    failure, never success (final audit ARCH-4; core.storage_purge)."""
    store = _store()
    if backend_name(store) != "s3":
        return 0

    def remove(_cancel: Any) -> int:
        try:
            result = storage_purge.purge(store, key)
        except storage_purge.PurgeFailed as exc:
            raise StorageUnavailable("Versions not deleted.", error_class=exc.error_class) from exc
        if not result.complete:
            raise StorageUnavailable(
                "Versions not deleted.",
                error_class="access_denied" if result.retained else "server_error",
            )
        return result.removed

    return int(_call("delete", remove))  # a delete, as the metrics count it


def size(key: str) -> int:
    """The object's size as the store reports it (ObjectMissing if there is none)."""
    return int(_call("size", lambda _cancel: _store().size(key)))


def iter_file(file: IO[bytes]) -> Iterator[bytes]:
    try:
        yield from iter(lambda: file.read(CHUNK_BYTES), b"")
    finally:
        file.close()


# --- listing (reconcile_attachments) --------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size: int


class ObjectPages:
    """Every object in the attachments store, a page at a time in key order (code points:
    S3's order, and the walk's), never all at once. Resumable: after a failed page the next
    one starts after the last key returned. The liveness probe's sentinels are left out."""

    def __init__(self, page_size: int) -> None:
        self.page_size = page_size
        self.after = ""
        self.done = False
        self._walk: Iterator[StoredObject] | None = None

    def next_page(self) -> list[StoredObject]:
        if self.done:
            return []
        store = _store()
        if backend_name(store) == "s3":
            page = _call("list", lambda _cancel: self._s3_page(store))
        else:
            page = _call("list", lambda _cancel: self._walk_page(store))
        if page:
            self.after = page[-1].key
        return page

    def _s3_page(self, store: Any) -> list[StoredObject]:
        location = str(store.location).strip("/")
        prefix = f"{location}/" if location else ""
        page: list[StoredObject] = []
        after = self.after
        while not page:  # a page of probe sentinels only: the next one
            response = store.connection.meta.client.list_objects_v2(
                Bucket=store.bucket_name,
                Prefix=prefix,
                MaxKeys=self.page_size,
                **({"StartAfter": prefix + after} if after else {}),
            )
            contents = response.get("Contents", [])
            page = [
                StoredObject(item["Key"][len(prefix) :], int(item["Size"]))
                for item in contents
                if not item["Key"][len(prefix) :].rpartition("/")[2].startswith(PROBE_PREFIX)
            ]
            if not response.get("IsTruncated") or not contents:
                self.done = True
                break
            after = contents[-1]["Key"][len(prefix) :]
        return page

    def _walk_page(self, store: Any) -> list[StoredObject]:
        if self._walk is None:
            self._walk = _walk(str(store.location), "", self.after)
        try:
            page = list(itertools.islice(self._walk, self.page_size))
        except BaseException:
            self._walk = None  # resumed after self.after
            raise
        if len(page) < self.page_size:
            self.done = True
        return page


def _walk(directory: str, prefix: str, after: str) -> Iterator[StoredObject]:
    """Files under `directory` whose relative path sorts after `after`, in code-point order
    of their paths: a directory sorts as its name plus "/". One directory listing in memory
    at a time; whole directories before `after` are skipped unread."""
    try:
        with os.scandir(directory) as found:
            entries = [
                (entry.name + "/" if entry.is_dir(follow_symlinks=False) else entry.name, entry)
                for entry in found
            ]
    except FileNotFoundError:
        return  # nothing stored yet (an empty volume is checked against the rows)
    entries.sort(key=lambda pair: pair[0])
    for sort_name, entry in entries:
        path = prefix + sort_name
        if sort_name.endswith("/"):
            if path < after and not after.startswith(path):
                continue
            yield from _walk(entry.path, path, after)
        elif path > after and entry.is_file(follow_symlinks=False):
            if not entry.name.startswith(PROBE_PREFIX):
                yield StoredObject(path, entry.stat(follow_symlinks=False).st_size)


# --- liveness (the metrics endpoint, the breaker's background probe) ------------------------------
def _probe() -> None:
    """Raises unless the store answers: S3 `head_bucket` (with the client's timeouts); any
    other store writes, reads back and deletes a small sentinel."""
    store = _store()
    if backend_name(store) == "s3":
        cast(Any, store).connection.meta.client.head_bucket(Bucket=cast(Any, store).bucket_name)
        return
    name = f"{PROBE_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"
    store.save(name, ContentFile(b"ok"))
    try:
        with store.open(name, "rb") as file:
            if file.read() != b"ok":
                raise _Integrity("The probe read back something else.")
    finally:
        store.delete(name)


_probe_lock = threading.Lock()
_probe_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="attachment-storage-probe")
_probe_state: dict[str, Any] = {"at": -math.inf, "up": False, "future": None}


def _timed_probe() -> None:
    started = time.monotonic()
    try:
        _probe()
    except Exception as error:
        telemetry.storage_call("probe", time.monotonic() - started, classify(error))
        raise
    telemetry.storage_call("probe", time.monotonic() - started, None)


def storage_up(timeout: float) -> bool:
    """Whether the store answers, for the metrics endpoint: a fresh check at most every
    PROBE_CACHE_S per process, waited for at most `timeout`. A check still running from an
    earlier scrape counts as down without waiting again. Never on a user's request."""
    with _probe_lock:
        now = time.monotonic()
        if now - _probe_state["at"] < PROBE_CACHE_S:
            return bool(_probe_state["up"])
        previous: Future[None] | None = _probe_state["future"]
        if previous is not None and not previous.done():
            _probe_state.update(at=now, up=False)
            return False
        future = _probe_pool.submit(_timed_probe)
        _probe_state["future"] = future
    try:
        future.result(timeout=timeout)
        up = True
    except Exception:  # noqa: BLE001 — a timeout or a failure: down
        up = False
    with _probe_lock:
        _probe_state.update(at=time.monotonic(), up=up)
    return up


def forget_probe() -> None:
    """Tests: the next storage_up() checks again."""
    with _probe_lock:
        _probe_state.update(at=-math.inf, up=False)


# --- scanning -------------------------------------------------------------------------------------
# One INSTREAM conversation per file, on a worker (the outbox job; never in a request). clamd
# speaks plain TCP without authentication: it must sit on loopback or the private network
# (production refuses any other host: core.transport.scanner_problem).
#
# The whole conversation has one wall-clock deadline, not a timeout per call: a scanner that
# drips its reply, or reads slowly, can't hold the worker past the task's soft time limit
# (CELERY_TASK_SOFT_TIME_LIMIT, 100 s; the object was fetched within the storage deadline,
# 12 s, before).
SCAN_DEADLINE_S = 75.0
SCAN_REPLY_MAX_BYTES = 4096
# Why a scan gave no verdict (ScannerUnavailable.reason). `size_limit`: clamd refused a stream
# over its StreamMaxLength, which must be at least ATTACHMENT_MAX_BYTES (docs/deployment.md):
# every such file stays unavailable until it is raised. `limits_exceeded`: clamd (with
# AlertExceedsMax) says the file is over one of its scan limits (MaxFileSize, MaxScanSize,
# MaxFiles, MaxRecursion, ...): not scanned whole, so not clean, and not malware either (the
# file is kept, unavailable, rather than deleted as infected). Without AlertExceedsMax, clamd
# answers OK for a file over MaxFileSize after scanning only its start: its limits must be at
# least ATTACHMENT_MAX_BYTES (tests/drills/clamav_drill.py proves them). `integrity`: the
# bytes scanned are not the file that was uploaded (its SHA-256 differs): never clean.
SCAN_FAILURES = (
    "unconfigured",
    "timeout",
    "connection",
    "size_limit",
    "error_reply",
    "protocol",
    "integrity",
    "limits_exceeded",
)
CLEAN_REPLY = "stream: OK"
_FOUND_REPLY = re.compile(r"stream: (?P<signature>.+) FOUND")
_SIGNATURE = re.compile(r"[A-Za-z0-9._:+/()-]{1,128}")
_LIMITS_EXCEEDED = "Heuristics.Limits.Exceeded"


@dataclass(frozen=True, slots=True)
class Verdict:
    clean: bool
    # What clamd found (empty when clean): a signature name such as "Win.Test.EICAR_HDB-1",
    # never anything from the file; "unknown" if the reply named it in another shape.
    signature: str = ""


def scanning_enabled() -> bool:
    return bool(settings.ATTACHMENT_SCANNER)


def _scanner_address(value: str) -> tuple[str, int] | None:
    parts = urlsplit(value)
    if parts.scheme != "clamd" or not parts.hostname:
        return None
    try:
        port = parts.port or 3310
    except ValueError:
        return None
    return parts.hostname, port


def scan(file: IO[bytes], *, expected_sha256: str | None = None) -> Verdict:
    """Ask the ClamAV daemon about the file (zINSTREAM): clean, or what it found. Fails
    closed: ScannerUnavailable (with its reason) whenever clamd didn't give one of its two
    verdicts in full, so the job retries and the file stays unavailable meanwhile. With
    `expected_sha256`, a clean verdict also needs the bytes sent to be that file."""
    address = _scanner_address(settings.ATTACHMENT_SCANNER)
    if address is None:
        raise ScannerUnavailable("unconfigured")
    deadline = time.monotonic() + SCAN_DEADLINE_S
    digest = hashlib.sha256()
    file.seek(0)
    try:
        connection = socket.create_connection(address, timeout=_scan_time_left(deadline))
    except OSError as error:
        raise ScannerUnavailable(_scan_failure(error)) from error
    with connection:
        try:
            _send_stream(connection, file, deadline, digest)
        except OSError as error:
            # clamd answers a stream over its StreamMaxLength with an error and closes the
            # connection while the file is still being sent: read what it said, if anything.
            said = _read_reply(connection, deadline, quiet=True)
            reason = _reply_failure(said) if said else _scan_failure(error)
            raise ScannerUnavailable(reason) from error
        try:
            reply = _read_reply(connection, deadline)
        except OSError as error:
            raise ScannerUnavailable(_scan_failure(error)) from error
    verdict = _verdict(reply)
    if verdict.clean and expected_sha256 is not None and digest.hexdigest() != expected_sha256:
        raise ScannerUnavailable("integrity")
    return verdict


def _scan_time_left(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("The scan's deadline passed.")
    return left


def _send_stream(connection: socket.socket, file: IO[bytes], deadline: float, digest: Any) -> None:
    connection.settimeout(_scan_time_left(deadline))
    connection.sendall(b"zINSTREAM\0")
    for chunk in iter(lambda: file.read(CHUNK_BYTES), b""):
        digest.update(chunk)
        connection.settimeout(_scan_time_left(deadline))
        connection.sendall(struct.pack("!L", len(chunk)) + chunk)
    connection.settimeout(_scan_time_left(deadline))
    connection.sendall(struct.pack("!L", 0))


def _read_reply(connection: socket.socket, deadline: float, *, quiet: bool = False) -> bytes:
    """clamd's reply up to its NUL terminator (bounded). `quiet`: whatever arrived before
    an error, the error swallowed (the connection already failed)."""
    reply = b""
    try:
        while not reply.endswith(b"\0") and len(reply) <= SCAN_REPLY_MAX_BYTES:
            connection.settimeout(_scan_time_left(deadline))
            part = connection.recv(SCAN_REPLY_MAX_BYTES)
            if not part:
                break
            reply += part
    except OSError:
        if not quiet:
            raise
    return reply


def _scan_failure(error: OSError) -> str:
    return "timeout" if isinstance(error, TimeoutError) else "connection"


def _reply_failure(reply: bytes) -> str:
    text = reply.rstrip(b"\0").decode("utf-8", "replace").strip()
    if "size limit exceeded" in text.lower():
        return "size_limit"
    if text.endswith("ERROR"):
        return "error_reply"
    return "protocol"


def _verdict(reply: bytes) -> Verdict:
    """Exactly "stream: OK" is clean, "stream: <signature> FOUND" is malware; a reply cut
    off before its terminator, or of any other shape, is no verdict."""
    if not reply:
        raise ScannerUnavailable("connection")  # closed without a word
    if not reply.endswith(b"\0") or reply.count(b"\0") != 1:
        raise ScannerUnavailable("protocol")
    text = reply[:-1].decode("utf-8", "replace").strip()
    if text == CLEAN_REPLY:
        return Verdict(clean=True)
    found = _FOUND_REPLY.fullmatch(text)
    if found is not None:
        name = found["signature"].strip()
        if name.startswith(_LIMITS_EXCEEDED):
            raise ScannerUnavailable("limits_exceeded")
        return Verdict(clean=False, signature=name if _SIGNATURE.fullmatch(name) else "unknown")
    raise ScannerUnavailable(_reply_failure(reply))
