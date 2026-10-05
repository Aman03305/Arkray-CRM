"""Attachment files: what is accepted, how it is received, where it is kept, how it is
scanned (docs/activities.md#attachments).

- **Types**: an explicit catalog of business documents and images the server can recognise
  by their *content*; ATTACHMENT_ALLOWED_EXTENSIONS enables a subset (a system check refuses
  anything outside the catalog). Executables, scripts, HTML, SVG, archives and everything
  else are refused, whatever their name or declared type. A file must be what its extension
  says: a PDF starts like a PDF, an image is that image format, Office files are the right
  OOXML package (without macros), text is UTF-8 without NUL bytes.
- **Names**: the uploaded name is display text only. It is cleaned (no path, no control or
  bidi characters, bounded) and never used for storage: objects live under generated keys.
- **Receiving**: the body is read from the request stream in chunks, hashed (SHA-256) and
  counted as it arrives; more than ATTACHMENT_MAX_BYTES stops the read at once. Spooled to a
  temporary file above ATTACHMENT_SPOOL_MEMORY_BYTES, never held whole in memory.
- **Storage**: STORAGES["attachments"] (a private directory locally, private S3-compatible
  storage in production). Failures surface as StorageUnavailable: the upload or download
  fails with a 503 and nothing else in the CRM depends on storage.
- **Scanning**: none, or a ClamAV daemon (clamd's INSTREAM protocol) when ATTACHMENT_SCANNER
  is set; never a pretend scanner.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import re
import socket
import struct
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import IO, Any, Protocol
from urllib.parse import unquote, urlsplit

from django.conf import settings
from django.core.checks import Error, register
from django.core.files import File
from django.core.files.storage import Storage, storages

from arkray.core.text import TextRejected, clean_line

logger = logging.getLogger(__name__)

CHUNK_BYTES = 64 * 1024
HEAD_BYTES = 8 * 1024
MAX_ZIP_ENTRIES = 10_000
LARGEST_FILE_BYTES = 100 * 1024 * 1024  # the activities_attachment_size CHECK
# Office packages: what may be embedded (documents and pictures; never OLE objects, ActiveX
# controls or macro projects, whatever their part is called), and the small XML parts read
# to find active content declared by type or relationship (bounded: a zip bomb stays cheap).
SAFE_EMBEDDINGS = (".xlsx", ".docx", ".pptx", ".png", ".jpeg", ".jpg", ".gif", ".emf", ".wmf")
ACTIVE_CONTENT_TYPES = (b"vbaproject", b"macroenabled", b"activex", b"oleobject", b"vbadata")
OOXML_XML_PART_BYTES = 256 * 1024
OOXML_XML_TOTAL_BYTES = 4 * 1024 * 1024


class StorageUnavailable(Exception):
    """The attachment store could not be reached or refused the operation."""


class ScannerUnavailable(Exception):
    """The malware scanner could not give an answer (retry later)."""


class RejectedFile(ValueError):
    """The file can't be accepted; the message is for the person who uploaded it."""


# --- the catalog ----------------------------------------------------------------------------------
def _starts(*prefixes: bytes) -> Callable[[bytes, IO[bytes]], bool]:
    return lambda head, _file: any(head.startswith(prefix) for prefix in prefixes)


def _webp(head: bytes, _file: IO[bytes]) -> bool:
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _ooxml(main_part: str) -> Callable[[bytes, IO[bytes]], bool]:
    """An Office Open XML package with its main part and no active content: no macro project,
    OLE object or ActiveX control (by part name, embedded file type, declared content type),
    no external template. The zip's central directory, then only its small XML parts
    ([Content_Types].xml, relationships; each and in total bounded) are read, so a zip bomb
    costs little here (enhancement security review: a renamed VBA part, an OLE package
    wrapping an executable, ActiveX all passed a name-only check)."""

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
        except (zipfile.BadZipFile, OSError, ValueError, EOFError, RuntimeError):
            return False

    return check


def _active_content(package: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> bool:
    budget = OOXML_XML_TOTAL_BYTES
    for info in infos:
        name = info.filename.lower()
        if name.endswith(".bin") and "/printersettings/" not in name:
            return True  # vbaProject.bin (under any name), oleObject*.bin, activeX*.bin
        if "/activex/" in name:
            return True
        if "/embeddings/" in name and not name.endswith(SAFE_EMBEDDINGS):
            return True
        declares = name == "[content_types].xml"
        if declares or name.endswith(".rels"):
            if info.file_size > OOXML_XML_PART_BYTES or info.file_size > budget:
                return True  # no real package has XML parts this large: refuse, unread
            budget -= info.file_size
            text = package.read(info).lower()
            if declares and any(kind in text for kind in ACTIVE_CONTENT_TYPES):
                return True
            if not declares and b"attachedtemplate" in text and b'targetmode="external"' in text:
                return True  # a remote template: fetched (macros and all) when opened
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
def _log_failure(operation: str, error: Exception) -> None:
    """An operational signal: which operation failed and the error's type (never a key, a
    name or content)."""
    logger.warning(
        "attachment_storage_failed", extra={"operation": operation, "error": type(error).__name__}
    )


def _store() -> Storage:
    return storages["attachments"]


def save(key: str, file: IO[bytes]) -> None:
    try:
        stored_as = _store().save(key, File(file))
    except Exception as error:
        logger.warning(
            "attachment_storage_failed", extra={"operation": "save", "error": type(error).__name__}
        )
        raise StorageUnavailable from error
    if stored_as != key:  # the backend renamed it: never expected with generated keys
        delete(stored_as)
        raise StorageUnavailable("The storage backend renamed the object.")


def open_file(key: str) -> IO[bytes]:
    try:
        return _store().open(key, "rb")
    except Exception as error:
        logger.warning(
            "attachment_storage_failed", extra={"operation": "open", "error": type(error).__name__}
        )
        raise StorageUnavailable from error


def delete(key: str) -> None:
    """Remove an object; one that is already gone counts as removed (idempotent)."""
    try:
        _store().delete(key)
    except FileNotFoundError:
        return
    except Exception as error:
        logger.warning(
            "attachment_storage_failed",
            extra={"operation": "delete", "error": type(error).__name__},
        )
        raise StorageUnavailable from error


def iter_file(file: IO[bytes]) -> Iterator[bytes]:
    try:
        yield from iter(lambda: file.read(CHUNK_BYTES), b"")
    finally:
        file.close()


# --- scanning -------------------------------------------------------------------------------------
SCAN_TIMEOUT_S = 60


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


def scan(file: IO[bytes]) -> bool:
    """Ask the ClamAV daemon about the file: True if clean, False if it found malware.
    ScannerUnavailable when there is no answer (the job retries)."""
    address = _scanner_address(settings.ATTACHMENT_SCANNER)
    if address is None:
        raise ScannerUnavailable("No scanner is configured.")
    file.seek(0)
    try:
        with socket.create_connection(address, timeout=SCAN_TIMEOUT_S) as connection:
            connection.sendall(b"zINSTREAM\0")
            for chunk in iter(lambda: file.read(CHUNK_BYTES), b""):
                connection.sendall(struct.pack("!L", len(chunk)) + chunk)
            connection.sendall(struct.pack("!L", 0))
            reply = b""
            while not reply.endswith(b"\0"):
                part = connection.recv(4096)
                if not part:
                    break
                reply += part
                if len(reply) > 4096:
                    break
    except OSError as error:
        raise ScannerUnavailable(type(error).__name__) from error
    answer = reply.rstrip(b"\0").decode("utf-8", "replace").strip()
    if answer.endswith(" OK"):
        return True
    if answer.endswith(" FOUND"):
        return False
    raise ScannerUnavailable("The scanner gave no verdict.")
