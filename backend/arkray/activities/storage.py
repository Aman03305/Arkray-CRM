"""Attachment files: what is accepted, how it is received, where it is kept, how it is
scanned (docs/activities.md#attachments).

- **Types**: an explicit catalog of business documents and images the server can recognise
  by their *content*; ATTACHMENT_ALLOWED_EXTENSIONS enables a subset (a system check refuses
  anything outside the catalog). Executables, scripts, HTML, SVG, archives and everything
  else are refused, whatever their name or declared type. A file must be what its extension
  says: a PDF starts like a PDF, an image is that image format, Office files are the right
  OOXML package (without macros, ActiveX, OLE objects or anything fetched from elsewhere when
  opened: its XML parsed, never searched as bytes; a Word document also without a field that
  starts a program or fetches content from elsewhere, a workbook without a formula that does;
  an embedded package held to the same rules, one level deep), text is UTF-8 without NUL bytes
  (a CSV also without a cell Excel would run as a DDE formula or a formula that fetches). A
  HYPERLINK is the one thing kept that reaches elsewhere, and only when someone clicks it.
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
import heapq
import io
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
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import IO, Any, NamedTuple, NoReturn, Protocol, cast
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import ParseError

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
# A real Office package stores its parts deflated or stored; it never uses bzip2 or LZMA. A
# member of one of those compressions is refused unread (R167, found while remediating the
# adversarial review of 73ff1e3): zipfile decompresses such a member whole on the first read
# whatever length is asked for (the max_length argument is honoured only for deflate), so a
# tiny member whose central-directory size is understated can expand to gigabytes and slip past
# the byte pre-checks below. Stored and deflated reads stay bounded, so only these two pass.
_ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
# Embedded objects (adversarial review of 73ff1e3, L7, R159, and its review round 1). Every part
# other than [Content_Types].xml and the relationships is judged by what it holds, whatever it is
# called and wherever it sits (Office activates an embedded package through a `package`
# relationship to any part): an OLE compound file (a renamed .doc/.xls with macros, an OLE
# object) is refused; an OOXML package is an embedded object, opened and checked one level deep
# with the same rules and its own content check (a document's fields, a workbook's formulas),
# under budgets shared by the whole upload; a package inside one, or a package under a picture's
# name, is refused. Under /embeddings/ a part must also be named as a picture or a
# .docx/.xlsx/.pptx (a .docm/.xlsm/.pptm, a .zip or a renamed executable is refused by name).
EMBEDDED_IMAGES = (".png", ".jpeg", ".jpg", ".gif", ".emf", ".wmf")
EMBEDDED_PACKAGES = (".xlsx", ".docx", ".pptx")
EMBEDDED_DEPTH = 1  # how deep an embedded package is opened: one level, never a package in one
# Their own byte budget (review round 1, PARSERS-4: an ordinary proposal with two 2.2 MB
# workbooks ran out of the 4 MiB XML budget): one embedded package is held in memory while it is
# checked, at most EMBEDDED_PACKAGE_BYTES of it; all of an upload's together at most
# EMBEDDED_TOTAL_BYTES. Past either, the file is "too large to check", never a type mismatch.
EMBEDDED_PACKAGE_BYTES = 16 * 1024 * 1024
EMBEDDED_TOTAL_BYTES = 32 * 1024 * 1024
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")  # a zip with entries, an empty zip
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # an OLE compound file
# Declared content types (and part names in [Content_Types].xml) that mean active content or
# something fetched from elsewhere when the file opens: macros, ActiveX, OLE objects, a
# workbook's links to other workbooks and its data connections, documents Word imports when it
# opens the file (an altChunk's RTF, which can carry OLE objects and self-updating links, or
# MHT/HTML, which can fetch remote pictures), Office add-ins (deep security audit review),
# Excel 4.0 (XLM) macro sheets, international ones too (ms-excel.macrosheet,
# ms-excel.intlmacrosheet: macros without a VBA project, R132).
ACTIVE_CONTENT_TYPES = (
    "vbaproject",
    "macroenabled",
    "macrosheet",
    "activex",
    "oleobject",
    "vbadata",
    "externallink",
    "spreadsheetml.connections",
    "spreadsheetml.querytable",  # a query table refreshes from a data source (review round 1)
    "application/rtf",
    "text/rtf",
    "message/rfc822",
    "text/html",
    "xhtml",
    "webextension",
    "taskpane",
)
_ACTIVE_CONTENT_TYPE = re.compile("|".join(map(re.escape, ACTIVE_CONTENT_TYPES)))  # one search
# Part names, by their ending, of documents Word would import (an altChunk's RTF, MHT, HTML).
IMPORTED_DOCUMENTS = (".rtf", ".mht", ".mhtml", ".htm", ".html", ".xhtml")
# Relationship types (anywhere in the Type URI, case ignored) refused whatever their target
# mode: OLE objects (CVE-2017-0199, "Follina"), remote templates, ActiveX, macro projects,
# master-document subdocuments, frames, links to other workbooks, data connections, imported
# documents (aFChunk: Word's altChunk), Office add-ins (webextension, webextensionTaskpanes:
# an add-in loads from its web address when the file opens), Excel 4.0 macro sheets
# (xlMacrosheet, xlIntlMacrosheet).
REFUSED_RELATIONSHIPS = (
    "oleobject",
    "attachedtemplate",
    "activex",
    "vbaproject",
    "macrosheet",
    "vbadata",
    "subdocument",
    "frame",
    "externallink",
    "connections",
    "querytable",
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


_Body = Callable[[zipfile.ZipFile, list[zipfile.ZipInfo], "_PackageBudget"], bool]


def _ooxml(main_part: str, body: _Body | None = None) -> Callable[[bytes, IO[bytes]], bool]:
    """An Office Open XML package with its main part and no active content: no macro project,
    OLE object or ActiveX control (by part name, content, declared content type, relationship
    type), nothing fetched from elsewhere when it opens (a remote template, a linked picture or
    object, a link to another workbook, a data connection), and no embedded package that has
    any (L7, R159). The zip's end records, its central directory (refused before zipfile parses
    it when it declares more than the entries budget, or its records are more than that or hold
    an oversized name, extra field or comment: _directory_too_large), its small XML parts
    ([Content_Types].xml, relationships; each and in total bounded) and the first bytes of every
    other part are read, so a zip bomb costs little here (enhancement security review: a renamed
    VBA part, an OLE package wrapping an executable, ActiveX all passed a name-only check). Then
    `body`, if any, reads the content itself (a Word document's fields: _word_fields; a
    workbook's formulas: _xlsx_formulas), bounded too, and may refuse with a message of its own
    (RejectedFile). Past a budget the file is "too large to check" (TOO_LARGE_TO_CHECK);
    anything that can't be read or parsed is refused (fail closed)."""

    def check(head: bytes, file: IO[bytes]) -> bool:
        if not head.startswith(b"PK\x03\x04"):
            return False
        file.seek(0)
        try:
            budget = _PackageBudget()
            if _directory_too_large(file, budget):
                raise _OverBudget
            file.seek(0)
            with zipfile.ZipFile(file) as package:
                infos = package.infolist()
                if len(infos) > MAX_ZIP_ENTRIES:
                    raise _OverBudget
                lowered = {info.filename.lower() for info in infos}
                if "[content_types].xml" not in lowered or main_part not in lowered:
                    return False
                if _active_content(package, infos, budget):
                    return False
                return body is None or body(package, infos, budget)
        except RejectedFile:
            raise  # refused with its own message (a Word field, a document too large to check)
        except _OverBudget:
            logger.info("attachment_package_check_refused", extra={"rule": "budget"})
            raise RejectedFile(TOO_LARGE_TO_CHECK) from None
        except Exception:  # noqa: BLE001 — a corrupt stream (zlib.error, ...), an encrypted
            return False  # entry, an unsupported compression: not a package we can vouch for

    return _bounded(check)


# Review round 2 (PARSERS2-2: a 70-260 KB upload cost 9-13 s, spending each of the checks' budgets
# in turn at its most expensive unit). The work one upload's check does is paid for from one pool
# of UPLOAD_WORK units, a unit being about a microsecond of CPU at the slowest measured shape on the
# development laptop (an i7-13700HX; production's CPUs are slower per core, the test bound is
# CPU_BOUND): reading a part, READ_BYTES_PER_WORK decompressed bytes a unit; parsing one,
# PARSE_BYTES_PER_WORK bytes fed a unit, plus 1 an element and 1 an attribute, plus PARSER_WORK for
# the parser itself; an entry, ENTRY_WORK; opening a member, OPEN_WORK; an embedded package,
# EMBEDDED_WORK and its bytes as read; judging a field code or a formula by its length. Each check
# keeps its own caps too (WORD_XML_BYTES and WORD_XML_WORK, SHEET_XML_BYTES and SHEET_XML_WORK),
# so a document's fields can't starve its workbook's formulas of more than their share. Measured
# after review round 3 (SCRATCH calibrate_r3): 0.15-1.0 us a unit at every shape the reviews found,
# the slowest being many tiny members and reading digits. Review round 3 (PARSERS3-1,
# PARSERS3-2) found paths the units did not price (a quadratic judge, the small XML parts parsed
# into trees and again by every check, members opened at one unit each), so the units are a bound
# only as good as their pricing: the CPU deadline below is the structural one.
UPLOAD_WORK = 1_000_000
READ_BYTES_PER_WORK = 160
PARSE_BYTES_PER_WORK = 64
ENTRY_WORK = 4  # an entry, looked at by every check that walks the package's entries
OPEN_WORK = 16  # opening a member and reading its first block (9-15 us)
PARSER_WORK = 48  # an expat parser: made, fed a small part, closed (20 us alone, 40-50 in place)
EMBEDDED_WORK = 96  # opening an embedded package: its directory, the zipfile objects
# Review round 3 (structural, defence in depth): every check of one upload runs against a deadline
# of UPLOAD_CPU_SECONDS of this thread's CPU from the moment it starts (_bounded), looked at in
# every loop that can run long (_on_time: each member, each chunk read or parsed, each field code
# or formula judged, each embedded package, each CSV chunk and judged cell); past it the upload is
# "too large to check". Thread CPU, not process CPU: a web worker's other threads (metrics,
# telemetry) don't count against an upload. The unit budgets stay the primary, deterministic bound
# (the same answer on any machine): an ordinary upload costs a fraction of a second here and the
# worst shapes the reviews found about one, so the deadline, five times that, is met only by a
# path the units fail to price: a production core three or four times slower than this laptop's
# (throttled cloud CPUs) still checks an upload at the unit budget in time, and an unpriced path
# still ends far inside the web worker's 30 s timeout (lead's choice over the implementer's 3 s).
UPLOAD_CPU_SECONDS = 5.0
_cpu_clock: Callable[[], float] = time.thread_time
_DEADLINE: ContextVar[float] = ContextVar("upload_check_deadline", default=math.inf)


def _on_time() -> None:
    """_OverBudget once this upload's check has run past its CPU deadline (outside a check,
    _bounded, there is none)."""
    if _cpu_clock() > _DEADLINE.get():
        raise _OverBudget


def _bounded(check: Callable[[bytes, IO[bytes]], bool]) -> Callable[[bytes, IO[bytes]], bool]:
    """`check`, run against a CPU deadline of UPLOAD_CPU_SECONDS (read when it starts): past it,
    whichever loop notices raises _OverBudget, answered "too large to check"."""

    def run(head: bytes, file: IO[bytes]) -> bool:
        token = _DEADLINE.set(_cpu_clock() + UPLOAD_CPU_SECONDS)
        try:
            return check(head, file)
        except _OverBudget:
            logger.info("attachment_check_refused", extra={"rule": "deadline"})
            raise RejectedFile(TOO_LARGE_TO_CHECK) from None
        finally:
            _DEADLINE.reset(token)

    return run


class _PackageBudget:
    """Budgets shared by an upload and every package embedded in it (depth 1), so a nested
    package cannot multiply the work (L7, R159): the work pool (UPLOAD_WORK), the XML bytes of
    relationships and content types decompressed in all, the entries looked at in all, the
    embedded packages' bytes (PARSERS-4), and the content checks' own budgets — a document's
    fields (_word_fields) and a workbook's formulas (_xlsx_formulas), both drawing on the pool —
    which an embedded document or workbook spends too (PARSERS-6). Any of them spent:
    _OverBudget, answered "too large to check". Also what each package's [Content_Types].xml
    declares (`types`), read once and kept for every check of that package (review round 3,
    PARSERS3-2: each check parsed it again). Read when an upload is checked, not when the module
    loads."""

    __slots__ = ("embedded_bytes", "entries", "fields", "formulas", "pool", "types", "xml_bytes")

    def __init__(self) -> None:
        self.pool = _Work(UPLOAD_WORK)
        self.xml_bytes = OOXML_XML_TOTAL_BYTES
        self.entries = MAX_ZIP_ENTRIES
        self.embedded_bytes = EMBEDDED_TOTAL_BYTES
        self.fields = _Budget(WORD_XML_BYTES, WORD_XML_WORK, self.pool)
        self.formulas = _Budget(SHEET_XML_BYTES, SHEET_XML_WORK, self.pool)
        self.types: dict[zipfile.ZipFile, list[_Declared]] = {}

    def spend(self, units: int) -> None:
        """`units` from the pool: _OverBudget past it."""
        self.pool.left -= units
        if self.pool.left < 0:
            raise _OverBudget


# Review round 2 (PARSERS2-3: zipfile parses every record of a central directory before the
# entries can be counted; 357,000 one-byte-name records fit in a 16 MiB embedded package, 2.2 s and
# 149 MiB from a 50 KB upload). A package whose end records declare more entries than the budget
# allows, or a directory larger than DIRECTORY_BYTES_PER_ENTRY a remaining entry (a record is at
# least 46 bytes; real ones, about 80-120 with their names), or than DIRECTORY_BYTES in all, is
# refused before zipfile reads a record. Review round 4 (PARSERS4-1: zipfile's parse of a record
# costs with its name, extra field and comment, and Python 3.12's extra-field decoder is quadratic
# in the field's length, 16 ms for one of 64 KiB; names of 65,000 "%" cost unquote as much in every
# pass; a 20-55 KB upload took 3.6-4.5 s, stopped by nothing but the deadline): the records are
# then walked with struct, as zipfile will walk them, and the package is "too large to check" when
# one holds a name, an extra field or a comment over DIRECTORY_FIELD_BYTES (Office writes names
# under 200 bytes and extra fields of at most 36), or when there are more records than entries
# left; the directory's bytes are paid for (PARSE_BYTES_PER_WORK a unit).
DIRECTORY_BYTES = 2 * 1024 * 1024
DIRECTORY_BYTES_PER_ENTRY = 128
DIRECTORY_FIELD_BYTES = 1024
_DIRECTORY_RECORD = b"PK\x01\x02"
_DIRECTORY_RECORD_BYTES = 46
_END_RECORD = b"PK\x05\x06"
_END_LOCATOR64 = b"PK\x06\x07"
_END_RECORD64 = b"PK\x06\x06"


def _central_directory(file: IO[bytes]) -> tuple[int, int] | None:
    """The entries and bytes a zip's central directory declares (_end_record), or None."""
    found = _end_record(file)
    return None if found is None else found[:2]


def _end_record(file: IO[bytes]) -> tuple[int, int, int] | None:
    """The entries and bytes a zip's central directory declares, and where its end record is,
    read the way zipfile reads them (zipfile._EndRecData: the end record at the very end, else the
    last one in the final 64 KiB and 22 bytes; then, when a ZIP64 locator comes right before it,
    the ZIP64 record at the locator's offset or right before the locator, which is then where the
    directory ends), or None if there is no end record. zipfile reads the directory from the end
    record's position less the directory's size."""
    file.seek(0, 2)
    size = file.tell()
    if size < 22:
        return None
    file.seek(size - 22)
    data = file.read(22)
    if data[:4] == _END_RECORD and data[-2:] == b"\x00\x00":
        location, record = size - 22, data
    else:
        start = max(size - 65535 - 22, 0)
        file.seek(start)
        data = file.read(65535 + 22)
        found = data.rfind(_END_RECORD)
        if found < 0 or len(data) - found < 22:
            return None
        location, record = start + found, data[found : found + 22]
    entries, directory = struct.unpack("<4s4H2LH", record)[4:6]
    if location >= 20:
        file.seek(location - 20)
        locator = file.read(20)
        if locator[:4] == _END_LOCATOR64:
            offset = struct.unpack("<4sLQL", locator)[2]
            for at in (offset, location - 20 - 56):
                if 0 <= at <= size - 56:
                    file.seek(at)
                    record64 = file.read(56)
                    if record64[:4] == _END_RECORD64:
                        entries, directory = struct.unpack("<4sQ2H2L4Q", record64)[7:9]
                        location = at
                        break
    return entries, directory, location


def _directory_too_large(file: IO[bytes], budget: _PackageBudget) -> bool:
    """Whether the zip's central directory is more than the entries left could hold: by what
    its end records declare (_end_record; DIRECTORY_BYTES, DIRECTORY_BYTES_PER_ENTRY), then by
    its records (_records_too_large), its bytes paid for from the pool. No end record, or records
    zipfile can't read: False (zipfile then refuses the file itself)."""
    declared = _end_record(file)
    if declared is None:
        return False
    count, size, location = declared
    entries = budget.entries
    if count > entries or size > min(DIRECTORY_BYTES, DIRECTORY_BYTES_PER_ENTRY * entries):
        return True
    budget.spend(size // PARSE_BYTES_PER_WORK)
    return _records_too_large(file, location - size, size, entries)


def _records_too_large(file: IO[bytes], start: int, size: int, entries: int) -> bool:
    """Whether the directory records zipfile will parse (`size` bytes from `start`, walked as
    zipfile._RealGetContents walks them) hold a name, an extra field or a comment over
    DIRECTORY_FIELD_BYTES, or are more than `entries`. Records zipfile can't read: False."""
    if start < 0:
        return False
    file.seek(start)
    data = file.read(size)
    at = 0
    records = 0
    while at < len(data):
        if data[at : at + 4] != _DIRECTORY_RECORD or len(data) - at < _DIRECTORY_RECORD_BYTES:
            return False
        fields = struct.unpack_from("<3H", data, at + 28)  # name, extra field, comment lengths
        records += 1
        if max(fields) > DIRECTORY_FIELD_BYTES or records > entries:
            return True
        at += _DIRECTORY_RECORD_BYTES + sum(fields)
    return False


def _active_content(
    package: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    budget: _PackageBudget | None = None,
    depth: int = 0,
) -> bool:
    """Whether the package holds, declares or links to active or external content. The XML
    is parsed, never searched as bytes (deep security audit M-3: single quotes, spaces around
    "=", a character reference such as attached&#84;emplate, or a UTF-16 part all got past a
    byte search). Every other part is judged by its first bytes (_held): an OLE compound file
    is refused, an OOXML package is opened and checked one level deep (_embedded_active).
    A part named twice is refused (review round 3, PARSERS3-2: OPC part names are equal ignoring
    case, percent-decoded, and Office refuses such a package; each copy was parsed by every check).
    The small XML parts are streamed, never built into trees (_scan_part), and what the content
    types declare is kept for the content checks (_content_types). _OverBudget once a shared
    budget is spent or the upload's deadline has passed."""
    if budget is None:
        budget = _PackageBudget()
    seen: set[str] = set()
    for info in infos:
        _on_time()
        name = unquote(info.filename).lower().lstrip("/")
        if name in seen:
            return True
        seen.add(name)
    for info in infos:
        _on_time()
        budget.entries -= 1
        budget.spend(ENTRY_WORK)
        if budget.entries < 0:
            raise _OverBudget  # more entries, across nesting, than any real package has
        if info.compress_type not in _ALLOWED_COMPRESSION:
            return True  # bzip2/LZMA: never Office, and a bomb that slips the byte checks (R167)
        # Office matches part names ignoring case; a percent-encoded name is matched decoded.
        name = unquote(info.filename).lower().lstrip("/")
        if name.endswith(".bin") and "/printersettings/" not in name:
            return True  # vbaProject.bin (under any name), oleObject*.bin, activeX*.bin
        if "/activex/" in name or "/externallinks/" in name or "/webextensions/" in name:
            return True
        if name.endswith(IMPORTED_DOCUMENTS):
            return True  # an altChunk's RTF, MHT or HTML, imported when Word opens the file
        if "/embeddings/" in name and not name.endswith(EMBEDDED_IMAGES + EMBEDDED_PACKAGES):
            return True  # a macro-enabled document, an archive, an executable, ...
        declares = name == "[content_types].xml"
        if declares or name.endswith(".rels"):
            scan = _read_small_part(package, info, budget, "types" if declares else "relationships")
            if scan is None or scan.active:
                return True  # too large, not well-formed, a DTD or entity, an encoding not
            if declares:  # allowed, or it declares or links to active content
                budget.types[package] = scan.declared
            continue
        held = _held(package, info, budget)
        if held == "ole":
            return True  # an OLE compound file, whatever its name (a .doc, an OLE object)
        if held == "package":
            if name.endswith(EMBEDDED_IMAGES) or depth >= EMBEDDED_DEPTH:
                return True  # a package under a picture's name, or a package in a package
            if _embedded_active(package, info, budget, depth):
                return True
        elif "/embeddings/" in name and name.endswith(EMBEDDED_PACKAGES):
            return True  # named as a package, but not one
    return False


def _held(package: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _PackageBudget) -> str:
    """What a part holds, from its first bytes: "package" (a zip), "ole" (an OLE compound file)
    or "" (anything else). A deflated read of 8 bytes decompresses a few KiB at most; opening
    the member costs OPEN_WORK."""
    if info.is_dir() or info.file_size == 0:
        return ""
    budget.spend(OPEN_WORK)
    with package.open(info) as part:
        first = part.read(8)
    if first.startswith(_ZIP_MAGIC):
        return "package"
    return "ole" if first == _OLE_MAGIC else ""


def _embedded_active(
    package: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _PackageBudget, depth: int
) -> bool:
    """Whether an embedded OOXML package (an object embedded in another) holds active content.
    It is read whole into memory with a hard cap (deflate bounds the read; the compression guard
    already refused bzip2/LZMA), its bytes taken from the embedded packages' own budget and from
    the work pool (_OverBudget past either), its central directory measured before it is opened
    (_directory_too_large), then checked one level deeper with the same rules, and with both
    content checks, which follow its declared content types whatever its parts are called
    (review round 2, PARSERS2-4: an embedded workbook whose main part was not xl/workbook.xml went
    unread): its workbook formulas always, its Word fields when it declares WordprocessingML or
    holds word/document.xml. Its content types are read once, by _active_content, and kept only
    while it is checked. Opening it costs EMBEDDED_WORK."""
    _on_time()
    if info.file_size > min(EMBEDDED_PACKAGE_BYTES, budget.embedded_bytes):
        raise _OverBudget  # it claims to be larger than an embedded package we will read
    budget.spend(EMBEDDED_WORK)
    with package.open(info) as part:
        raw = part.read(EMBEDDED_PACKAGE_BYTES + 1)
    budget.embedded_bytes -= len(raw)
    budget.spend(len(raw) // READ_BYTES_PER_WORK)
    if len(raw) > EMBEDDED_PACKAGE_BYTES or budget.embedded_bytes < 0:
        raise _OverBudget
    buffer = io.BytesIO(raw)
    if _directory_too_large(buffer, budget):
        raise _OverBudget
    buffer.seek(0)
    try:
        inner = zipfile.ZipFile(buffer)
    except (zipfile.BadZipFile, ValueError, OSError):
        return True  # not a readable package
    with inner:
        try:
            inner_infos = inner.infolist()
            if len(inner_infos) > budget.entries:
                raise _OverBudget
            names: set[str] = set()
            for item in inner_infos:
                _on_time()
                names.add(unquote(item.filename).lower().lstrip("/"))
            if "[content_types].xml" not in names:
                return True  # no OPC package: nothing Office would open, nothing we can vouch for
            if _active_content(inner, inner_infos, budget, depth + 1):
                return True
            if not _xlsx_formulas(inner, inner_infos, budget):
                return True
            stories = _word_stories(inner, inner_infos, budget)
            if stories is None:
                return True
            if (stories[0] or stories[1] or "word/document.xml" in names) and not _word_fields(
                inner, inner_infos, budget
            ):
                return True
        finally:
            budget.types.pop(inner, None)
    return False


def _declaration_agrees(text: str, allowed: set[str]) -> bool:
    """Whether the XML declaration at the start of `text` (if any) is well formed and names
    no encoding, or one of `allowed` (the one the bytes were decoded with)."""
    if not text.startswith("<?xml"):
        return True
    declaration = _XML_DECLARATION.match(text)
    if declaration is None:
        return False
    if "encoding" in declaration[0]:
        declared = _DECLARED_ENCODING.search(declaration[0])
        if declared is None or declared["name"].strip().casefold() not in allowed:
            return False
    return True


def _local(name: str) -> str:
    """An element's or attribute's name without its namespace, case ignored."""
    return name.rpartition("}")[2].casefold()


SMALL_PART_ELEMENTS = 20_000


class _Declared(NamedTuple):
    """One declaration in [Content_Types].xml: its content type (lower case, parameters dropped)
    and the part name (an Override) or extension (a Default) it is made for, as Office matches
    them (case ignored, percent-decoded, no leading "/" or ".")."""

    kind: str
    name: str | None
    extension: str | None


class _PartScan:
    """_stream_story's target for [Content_Types].xml ("types") or a relationships part
    ("relationships"). Review round 3, PARSERS3-2: both were parsed whole into ElementTree trees
    (1-1.5 us and some 200 bytes an element: a 2 KB upload of four 1 MiB element-dense content
    types cost 4-6 s and 75-128 MiB), and the content types again by every content check. Now
    each element is looked at as it is parsed and forgotten: a declared type or part name, or
    text, that is active content (ACTIVE_CONTENT_TYPES), or a relationship of a refused type or
    pointing outside the package (_link_active); what the content types declare is kept
    (`declared`, _content_types). Bounds: an element is 1 work and 1 per attribute (with what
    _stream_story charges for the bytes and the parser), at most SMALL_PART_ELEMENTS elements (a
    1 MiB part of real declarations or relationships holds fewer than 10,000), XML_ATTRIBUTES
    attributes on one, XML_DEPTH levels; past any, _OverBudget."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.work = 0
        self.depth = 0
        self.elements = 0
        self.names: set[str] = set()
        self.text_switch: Callable[[bool], None] = _no_switch
        self.active = False
        self.declared: list[_Declared] = []
        self.text: list[str] = []

    def on_start(self, tag: str, attributes: list[str]) -> None:
        self.depth += 1
        self.elements += 1
        self.work += 1 + (len(attributes) >> 1)
        if (
            len(attributes) > 2 * XML_ATTRIBUTES
            or self.depth > XML_DEPTH
            or self.elements > SMALL_PART_ELEMENTS
        ):
            raise _OverBudget
        if self.kind == "types":
            if self.elements == 1:
                self.text_switch(True)  # a type written as an element's text counts too
            self._settle()
            self._declare(attributes)
        elif not self.active:
            self.active = _link_active(attributes)

    def on_end(self, tag: str) -> None:
        self.depth -= 1
        self._settle()

    def on_text(self, text: str) -> None:
        self.text.append(text)

    def on_namespace(self, prefix: str | None, uri: str | None) -> None:
        self.work += 1

    def on_namespace_end(self, prefix: str | None) -> None:
        pass

    def on_other(self, *_: object) -> None:
        self.work += 1

    def _settle(self) -> None:
        if self.text:
            if _ACTIVE_CONTENT_TYPE.search("".join(self.text).casefold()):
                self.active = True
            self.text = []

    def _declare(self, attributes: list[str]) -> None:
        declared: dict[str, str] = {}
        for index in range(0, len(attributes) - 1, 2):
            value = attributes[index + 1]
            if _ACTIVE_CONTENT_TYPE.search(value.casefold()):
                self.active = True
            declared[_local(attributes[index])] = value
        kind = declared.get("contenttype", "").partition(";")[0].strip().casefold()
        if kind:
            name = declared.get("partname")
            extension = declared.get("extension")
            self.declared.append(
                _Declared(
                    kind,
                    None if name is None else unquote(name).strip().lower().lstrip("/"),
                    None if extension is None else extension.strip().lower().lstrip("."),
                )
            )


def _link_active(attributes: list[str]) -> bool:
    """One element of a relationships part: a relationship of a refused type, or pointing outside
    the package (TargetMode not "Internal", or a target that is a URL or a UNC path) unless it is
    a hyperlink. Every attribute is looked at, namespaced or not, so a variant Office might honour
    can't slip past by its spelling."""
    types: list[str] = []
    modes: list[str] = []
    targets: list[str] = []
    for index in range(0, len(attributes) - 1, 2):
        found = {"type": types, "targetmode": modes, "target": targets}.get(
            _local(attributes[index])
        )
        if found is not None:
            found.append(attributes[index + 1].strip().casefold())
    if not (types or modes or targets):
        return False
    if any(word in kind for kind in types for word in REFUSED_RELATIONSHIPS):
        return True
    names = {kind.rstrip("/").rpartition("/")[2] for kind in types}
    if names & REFUSED_RELATIONSHIP_NAMES:
        return True
    if types and set(types) <= EXTERNAL_ALLOWED:
        return False  # a hyperlink, wherever it points
    if any(mode != "internal" for mode in modes):
        return True  # "External" (an image, a video, an object or a data source)
    return any(_ELSEWHERE.match(target) for target in targets)


def _scan_part(raw: bytes, budget: _PackageBudget, kind: str) -> _PartScan | None:
    """`raw`, a [Content_Types].xml ("types") or relationships part, streamed through a
    _PartScan: decoded as _decoded decodes (UTF-8, or UTF-16 with its byte-order mark; a
    declared encoding must agree; no U+0000), parsed by defusedxml's expat parser (no DTD,
    entity or external reference), its cost taken from the upload's pool. None if it can't be
    read the way Office reads it (not well-formed, a DTD or entity, an encoding not allowed)."""
    scan = _PartScan(kind)
    if not _stream_story(io.BytesIO(raw), _Budget(0, UPLOAD_WORK, budget.pool), scan):
        return None
    return scan


def _read_small_part(
    package: zipfile.ZipFile, info: zipfile.ZipInfo, budget: _PackageBudget, kind: str
) -> _PartScan | None:
    """A [Content_Types].xml or relationships part, read (OPEN_WORK; at most
    OOXML_XML_PART_BYTES each and OOXML_XML_TOTAL_BYTES for the upload, _OverBudget past the
    total) and scanned (_scan_part). None, to refuse the package, if it is larger than any real
    package's or can't be read."""
    if info.file_size > OOXML_XML_PART_BYTES:
        return None  # no real package has XML parts this large: refuse, unread
    if info.file_size > budget.xml_bytes:
        raise _OverBudget
    budget.spend(OPEN_WORK)
    with package.open(info) as part:
        raw = part.read(OOXML_XML_PART_BYTES + 1)  # bounded whatever the header says
    budget.xml_bytes -= len(raw)
    if len(raw) > OOXML_XML_PART_BYTES:
        return None
    if budget.xml_bytes < 0:
        raise _OverBudget
    budget.spend(len(raw) // READ_BYTES_PER_WORK)
    return _scan_part(raw, budget, kind)


def _content_types(
    package: zipfile.ZipFile, infos: list[zipfile.ZipInfo], budget: _PackageBudget
) -> list[_Declared] | None:
    """What the package's [Content_Types].xml declares: as _active_content kept it (once a
    package, review round 3, PARSERS3-2), else read now; [] without one, None if it can't be
    read."""
    declared = budget.types.get(package)
    if declared is not None:
        return declared
    for info in infos:
        if unquote(info.filename).lower().lstrip("/") == "[content_types].xml":
            scan = _read_small_part(package, info, budget, "types")
            if scan is None:
                return None
            budget.types[package] = scan.declared
            return scan.declared
    return []


# --- Word fields ----------------------------------------------------------------------------------
# A field is an instruction Word evaluates (R132, the deep security audit's M-3 residual;
# docs/activities.md#attachments). Most lay out or number the document (PAGE, TOC, REF,
# HYPERLINK, MERGEFIELD, DATE ...); a few start a program or fetch content from another place:
# DDE and DDEAUTO (a program, through Dynamic Data Exchange), INCLUDETEXT, INCLUDE (its old
# name), INCLUDEPICTURE, IMPORT and LINK (a file, picture or object from wherever their
# argument names: a web address or a network share sends the reader's address, and on Windows
# their sign-in, to that server when the field updates), DATABASE (a query against whatever
# data source, local or a remote server, its connection string names). Word keeps each story
# (the body, a header, a footer, a footnote, an endnote, a comment, a text box, a building
# block) as one stream of characters in which a field is begin, field code, separator,
# result, end; the XML writes it as w:fldChar begin/separate/end around w:instrText runs (the
# code split across any number of runs, other fields nested in it), or as one w:fldSimple with
# the code in its w:instr. Every part of a Word document that could be a story is read whole,
# decoded as _decoded decodes; one whose text mentions field markup (_FIELD_MARKUP) is then
# parsed as that stream: through defusedxml's expat parser (no DTD or entities), streamed,
# never searched as bytes, each field's code judged once complete (_field_rule).
#
# Budget, for the whole document; past it the document is refused ("too large to check"),
# never let through unread (the check runs inside the upload request, on a web worker):
# - WORD_XML_BYTES decompressed: 64 MiB of story XML is thousands of pages of text (Word
#   writes some 25-35 bytes of XML per element), far more than a 10 MB upload holds once its
#   pictures are counted; reading that much takes about 0.4 s, and a zip bomb stops there.
# - WORD_XML_WORK for parsing (expat calls back into Python for every element): an element
#   counts 1 and 1 per attribute; a namespace declaration, a comment, a CDATA section, an
#   mc:Ignorable prefix 1; field content FIELD_WORK per reading; judging a field's code 1 per
#   FIELD_CODE_CHARS_PER_WORK characters, per reading; since review round 2, the bytes parsed
#   (PARSE_BYTES_PER_WORK), and since review round 3, the parser and the members opened. A unit
#   of a real document costs under a microsecond at full clock; the reviews found shapes that
#   cost more per unit, each held within the bound by the bytes and elements charged with it
#   (calibrated in each round; the CPU deadline, UPLOAD_CPU_SECONDS, stops any shape still
#   unpriced), so 1 million bound parsing to about a second of CPU, what an authenticated user
#   can make a web worker spend per upload (measured worst: 0.7 s, a 63 MiB document read whole
#   and parsed to the budget); a real document with a field in every paragraph reaches it at
#   about 10.6 MiB of XML (some 500 pages). What expat itself
#   holds is bounded below (XML_TOKEN_BYTES ...): the largest traced memory any shape needed
#   was 3.2 MiB (docs/activities.md#attachments).
WORD_XML_BYTES = 64 * 1024 * 1024
WORD_XML_WORK = 1_000_000
FIELD_WORK = 3
FIELD_CODE_CHARS = 64 * 1024  # of one field's code (an EndNote or Zotero citation is a few KB)
FIELD_DEPTH = 64  # fields open inside one another (documents nest a few)
# Read with breaks joined and blanks and computed parts removed, a code starting with one of
# these is one of those fields, whatever separates its letters.
DDE_FIELDS = ("DDE",)  # DDE, DDEAUTO
FETCHING_FIELDS = ("INCLUDE", "IMPORT", "LINK")  # INCLUDETEXT, INCLUDEPICTURE, INCLUDE ...
FIELD_REFUSED = (
    "This document has a field that can start a program or fetch content from another place "
    "(DDE, or INCLUDETEXT, INCLUDEPICTURE or LINK with a web or network address). Remove it, "
    "or turn the fields into plain text (select all, then Ctrl+Shift+F9), and upload it again."
)
TOO_LARGE_TO_CHECK = (
    "This document is too large to check for active content. Split it into smaller documents."
)
# Stand, in a field code, for what can't be read without evaluating the document (a field
# nested in it: its result becomes part of the code; a symbol) and for a break (a tab, a line
# or paragraph break, which Word may read as a blank or as nothing). Never in decoded XML text:
# a part holding U+0000 is refused, and XML can't hold U+0001.
_COMPUTED = "\x00"
_BREAK = "\x01"
# The field's name: a literal word (or "=", a formula) at the start of its code.
_FIELD_NAME = re.compile(r"\s*(?:(?P<name>[A-Za-z]+)(?=[\s\\\"]|\Z)|=)")
# An argument naming another place, wherever an argument can start (the code's start, after a
# blank, at or after a quote: a superset of where Word starts one): a URL (a scheme of two
# characters or more: one letter and a colon is a drive) or a UNC or protocol-relative path, in
# any mix of slashes, escaped or not. One search, in C, linear in the code.
_ELSEWHERE_ARGUMENT = re.compile(
    r"""(?:^|(?<=[\s"]))(?:[\\/]{2}|[a-z][a-z0-9+.-]+:)""", re.IGNORECASE
)
_LEADING = re.compile(r"[\s\x01]*+")  # blanks and breaks before the name
_SEEN = re.compile(r"[^\s\x00\x01]")  # what the bare reading keeps
FIELD_CODE_CHARS_PER_WORK = 8  # judging a code: 1 unit of work per 8 characters, per reading


def _field_rule(code: str, *, complete: bool = True) -> str | None:
    """Why a field is refused, from its code (letter case doesn't matter), or None.
    `complete`: False when the code was cut at FIELD_CODE_CHARS. The code is read three ways,
    so how Word reads a break can't matter: with breaks as blanks, with breaks joining what is
    around them, and bare (blanks, breaks and computed parts removed: "DD", a tab, "EAUTO" is
    DDEAUTO to the bare reading, and DDE to the first).

    - "computed": a name that isn't a literal word (a nested field, a quoted string, a symbol:
      the 2017 DDE obfuscation spelled the name with nested QUOTE fields), or one of the
      fetching fields with an argument that is (a nested MERGEFIELD naming the picture), or
      whose code was cut: it can't be judged without evaluating the document, so it fails
      closed.
    - "dde": bare, the code starts with DDE (DDE, DDEAUTO), always (Word asks, and current
      versions don't start the program by default; a document has no business asking).
    - "database": bare, it starts with DATABASE, always (its connection string can name any
      server).
    - "remote": bare, it starts with INCLUDE, IMPORT or LINK (INCLUDETEXT, INCLUDEPICTURE, the
      old INCLUDE, IMPORT, LINK) and an argument, read either way, names another place: a URL
      of any scheme (http:, file:, mhtml:, ...) or a UNC path (\\\\host, //host). A drive or
      root path (C:\\..., /Users/...) or a relative one passes: it names the reader's own disk,
      which the document's author doesn't control (the file would have to be there already),
      and ordinary documents carry them (an Excel range pasted as a link, a picture pasted from
      a web page or an e-mail)."""
    start = _LEADING.match(code)
    if start is None or start.end() == len(code):
        return None
    if _FIELD_NAME.match(code[start.end() : start.end() + 256].replace(_BREAK, " ")) is None:
        return "computed"
    bare = "".join(seen[0] for seen in itertools.islice(_SEEN.finditer(code), 8)).upper()
    if bare.startswith(DDE_FIELDS):
        return "dde"
    if bare.startswith("DATABASE"):
        return "database"
    if bare.startswith(FETCHING_FIELDS):
        if _ELSEWHERE_ARGUMENT.search(code.replace(_BREAK, " ")) or (
            _BREAK in code and _ELSEWHERE_ARGUMENT.search(code.replace(_BREAK, ""))
        ):
            return "remote"
        if _COMPUTED in code or not complete:
            return "computed"
    return None


@dataclass(slots=True)
class _Field:
    """A complex field being read: its code so far (bounded, blanks before the name dropped)."""

    parts: list[str] = field(default_factory=list)
    size: int = 0
    complete: bool = True
    in_code: bool = True  # no separator yet

    def add(self, text: str) -> None:
        if not self.size:
            text = text.lstrip()
        room = FIELD_CODE_CHARS - self.size
        if len(text) > room:
            text, self.complete = text[:room], False
        if text:
            self.parts.append(text)
            self.size += len(text)

    def copy(self) -> _Field:
        return _Field(list(self.parts), self.size, self.complete, self.in_code)

    def rule(self) -> str | None:
        return _field_rule("".join(self.parts), complete=self.complete)


@dataclass(slots=True)
class _Scope:
    """Where fields begin and end: a story (Word never joins its fields with anything outside
    it) or an mc:AlternateContent branch (inline: Word reads one of the branches, and which
    one depends on its version)."""

    story: bool
    in_code: bool = False  # a branch that opened inside a field code
    fields: list[_Field] = field(default_factory=list)  # open, innermost last
    stray: _Field | None = None  # w:instrText outside any field code, judged as one too


class _Reading:
    """One reading of a part's fields: as recorded, with every tracked change accepted
    (deleted and moved-away content skipped: `skips` "deleted") or rejected ("inserted").
    What Word may or may not see must not change how a field reads, so whatever can make two
    readings differ is either read every way or refused as "structure": a field code that runs
    into an AlternateContent branch, a field begun in a branch that doesn't end there or ending
    in one a field begun outside, field content in an element of a namespace the document
    declares ignorable (Word skips those it doesn't understand), fields nested past
    FIELD_DEPTH."""

    def __init__(
        self, report: Callable[[str], None], charge: Callable[[int], None], skips: str = ""
    ) -> None:
        self.report = report
        self.charge = charge  # judging a code costs work too (FIELD_CODE_CHARS_PER_WORK)
        self.skips = skips
        self.scopes = [_Scope(story=True)]

    def clone(self, skips: str) -> _Reading:
        other = _Reading(self.report, self.charge, skips)
        other.scopes = [
            _Scope(
                scope.story,
                scope.in_code,
                [open_field.copy() for open_field in scope.fields],
                None if scope.stray is None else scope.stray.copy(),
            )
            for scope in self.scopes
        ]
        return other

    def in_code(self) -> bool:
        scope = self.scopes[-1]
        return scope.in_code or bool(scope.fields and scope.fields[-1].in_code)

    def begin(self) -> None:
        scope = self._settle()
        if scope.fields and scope.fields[-1].in_code:
            scope.fields[-1].add(_COMPUTED)  # a nested field: its result is part of the code
        if len(scope.fields) >= FIELD_DEPTH:
            self.report("structure")
            return
        scope.fields.append(_Field())

    def separate(self) -> None:
        scope = self._settle()
        if not scope.fields:
            self._unmatched(scope)
            return
        current = scope.fields[-1]
        if current.in_code:
            current.in_code = False
            self._judge(current)

    def end(self) -> None:
        scope = self._settle()
        if not scope.fields:
            self._unmatched(scope)
            return
        current = scope.fields.pop()
        if current.in_code:
            self._judge(current)

    def text(self, text: str, *, code: bool) -> None:
        """Characters: `code` for w:instrText (a field code wherever it is), else run text or
        a symbol (part of a field code only inside one)."""
        scope = self.scopes[-1]
        if scope.in_code:
            self.report("structure")
        elif scope.fields and scope.fields[-1].in_code:
            scope.fields[-1].add(text)
        elif code:
            if scope.stray is None:
                scope.stray = _Field()
            scope.stray.add(text)

    def enter(self, *, story: bool) -> None:
        self.scopes.append(_Scope(story=story, in_code=not story and self.in_code()))

    def leave(self) -> None:
        scope = self.scopes.pop()
        self._flush(scope)
        if scope.fields and not scope.story:
            self.report("structure")  # begun in the branch, it would go on outside it
        for unfinished in scope.fields:
            if unfinished.in_code:
                self._judge(unfinished)

    def close(self) -> None:
        while self.scopes:
            self.leave()

    def _settle(self) -> _Scope:
        scope = self.scopes[-1]
        if scope.in_code:
            self.report("structure")  # a field in a branch that opened inside a field code
        self._flush(scope)
        return scope

    def _unmatched(self, scope: _Scope) -> None:
        if not scope.story:
            self.report("structure")  # it would act on a field begun outside the branch

    def _flush(self, scope: _Scope) -> None:
        if scope.stray is not None:
            self._judge(scope.stray)
            scope.stray = None

    def _judge(self, judged: _Field) -> None:
        _on_time()
        self.charge(1 + judged.size // FIELD_CODE_CHARS_PER_WORK)
        rule = judged.rule()
        if rule is not None:
            self.report(rule)


_W = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",  # Strict
)
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
# expat's names: namespace URI and local name joined by "}". Matched exactly, as Word does.
# Run text in a field code (w:t, not only w:instrText) is code to Word; a symbol (w:sym) can
# show as any character; a tab, a line break or a paragraph mark in a code is a break
# (_BREAK: _field_rule reads it both as a blank and as nothing).
_WORD_ELEMENTS: dict[str, str] = {
    f"{namespace}}}{local}": kind
    for namespace in _W
    for local, kind in (
        ("fldChar", "fldChar"),
        ("fldSimple", "fldSimple"),
        ("instrText", "instrText"),
        ("delInstrText", "instrText"),
        ("t", "t"),
        ("delText", "t"),
        ("sym", "sym"),
        ("tab", "break"),
        ("ptab", "break"),
        ("br", "break"),
        ("cr", "break"),
        ("p", "p"),
        ("del", "deleted"),
        ("moveFrom", "deleted"),
        ("ins", "inserted"),
        ("moveTo", "inserted"),
        *(
            (story, "story")
            for story in (
                "body",
                "hdr",
                "ftr",
                "footnote",
                "endnote",
                "comment",
                "docPartBody",
                "txbxContent",
            )
        ),
    )
} | {f"{_MC}}}Choice": "branch", f"{_MC}}}Fallback": "branch"}
# Attributes: Word reads them qualified. An unqualified w:instr is read as well (one more
# code judged); an unqualified fldCharType isn't (a fldChar without one opens a field).
_FIELD_CHAR_TYPE = frozenset(f"{namespace}}}fldCharType" for namespace in _W)
_FIELD_INSTRUCTION = frozenset({"instr", *(f"{namespace}}}instr" for namespace in _W)})
_IGNORABLE = f"{_MC}}}Ignorable"
_IGNORABLE_NAME = frozenset({_IGNORABLE})
_XML_SPACE = frozenset({"http://www.w3.org/XML/1998/namespace}space"})
_XML_BLANKS = " \t\r\n"
# Bounds on the XML itself, whatever the budget (past one, the part is refused as too large
# to check): what expat holds whole before it calls back (a start tag with its attributes,
# an attribute value, a comment) still unparsed past XML_TOKEN_BYTES; attributes on one
# element; elements, and mc:Ignorable scopes, open inside one another; distinct names
# (elements, attributes, prefixes: expat keeps a table entry for each). Word writes a few
# dozen attributes on an element, nests a few dozen levels, uses a few hundred names.
XML_TOKEN_BYTES = 1024 * 1024
XML_NAMES = 10_000
XML_ATTRIBUTES = 1_000
XML_DEPTH = 1_000
IGNORABLE_DEPTH = 32
# A scope kept is paid for beyond what it adds: its start and its end each empty the name memo
# (`kinds`), which the elements after it fill again. Sibling scopes cost about 2.3x a real
# document per unit of work without it (independent review, third pass).
IGNORABLE_SCOPE_WORK = 16


def _values(attributes: list[str], names: frozenset[str]) -> list[str]:
    """expat's attributes (name, value, name, value, ...): the values of `names`."""
    return [
        attributes[index + 1]
        for index in range(0, len(attributes) - 1, 2)
        if attributes[index] in names
    ]


def _no_switch(on: bool) -> None:
    """A story's text switch before _stream_story sets one."""


class _Story:
    """expat's handlers for one part: its fields as each reading sees them (as recorded, then,
    from the first field content inside a tracked change on, also with every change accepted
    and every change rejected), what Word may skip (an element of a namespace declared
    ignorable), and what reading it costs (`work`, for the budget: an element is 1 and 1 per
    attribute, field content FIELD_WORK per reading, judging a code by its length: the
    readings `charge` it). `report(rule)` is told what is
    refused (_field_rule's rules, or "structure"); _OverBudget past the XML's bounds. Ends
    are matched by tag (expat only calls with well-formed XML)."""

    def __init__(self, report: Callable[[str], None]) -> None:
        self.report = report
        self.readings = [_Reading(report, self._charge)]
        self.live = False  # a reading is in a field code: run text counts
        self.work = 0
        self.depth = 0
        self.collecting: bool | None = None  # in a w:instrText (True) or field-code w:t (False)
        self.trimming: list[str] | None = None  # its text, held to trim its blanks
        self.trimmed = 0
        self.deleted = 0
        self.inserted = 0
        self.ignored = 0
        self.prefixes: dict[str, list[str]] = {}
        self.ignorable: list[tuple[int, frozenset[str]]] = []
        self.ignorable_namespaces: frozenset[str] = frozenset()
        self.kinds: dict[str, str] = {}  # tag -> _kind(tag)
        self.names: set[str] = set()  # every distinct name the part used
        self.text_switch: Callable[[bool], None] = _no_switch

    def on_start(self, tag: str, attributes: list[str]) -> None:
        self.depth += 1
        self.work += 1 + (len(attributes) >> 1)
        if len(attributes) > 2 * XML_ATTRIBUTES or self.depth > XML_DEPTH:
            raise _OverBudget
        if attributes and _IGNORABLE in attributes:
            self._declare_ignorable(attributes)
        kind = self.kinds.get(tag)
        if kind is None:
            kind = self._kind(tag)
        if not kind:
            return
        if kind == "hidden":
            self.ignored += 1
        elif kind == "t":
            if self.live:
                self._content()
                self._collect(attributes, code=False)
        elif kind == "deleted":
            self.deleted += 1
        elif kind == "inserted":
            self.inserted += 1
        elif kind == "fldChar":
            values = set(_values(attributes, _FIELD_CHAR_TYPE))
            if len(values) > 1:
                self.report("structure")  # Transitional and Strict disagree
            value = values.pop() if len(values) == 1 else None
            for reading in self._here():
                # Only Word's exact "separate" and "end" close anything: any other value
                # opens a field (one more field read than Word does only adds to what is
                # judged; one fewer could join two of Word's fields into one that passes).
                if value == "separate":
                    reading.separate()
                elif value == "end":
                    reading.end()
                else:
                    reading.begin()
            self._changed()
        elif kind == "fldSimple":
            codes = _values(attributes, _FIELD_INSTRUCTION)
            if len(codes) > 1:
                self.report("structure")  # which one Word reads isn't certain
            code = codes[0] if codes else ""
            for reading in self._here():
                reading.begin()
                reading.text(code, code=True)
                reading.separate()
            self._changed()
        elif kind == "instrText":
            self._content()
            self._collect(attributes, code=True)
        elif kind == "break":
            if self.live:
                for reading in self._here():
                    reading.text(_BREAK, code=False)
        elif kind == "sym":
            if self.live:  # a symbol in a field code: it can show as any character
                for reading in self._here():
                    reading.text(_COMPUTED, code=False)
        elif kind != "p":  # "story", "branch"
            for reading in self._here():
                reading.enter(story=kind == "story")
            self._changed()

    def on_end(self, tag: str) -> None:
        kind = self.kinds.get(tag)
        if kind is None:
            kind = self._kind(tag)
        if kind:
            self._ended(kind)
        if self.ignorable and self.ignorable[-1][0] == self.depth:
            self.ignorable_namespaces = self.ignorable.pop()[1]
            self.kinds.clear()
        self.depth -= 1

    def _ended(self, kind: str) -> None:
        if kind == "hidden":
            self.ignored -= 1
        elif kind == "p":
            if self.live:  # a paragraph mark inside a field code
                for reading in self._here():
                    reading.text(_BREAK, code=False)
        elif kind in ("t", "instrText"):
            if self.collecting is not None and self.trimming is not None:
                self._dispatch("".join(self.trimming).strip(_XML_BLANKS))
            self.collecting = self.trimming = None
            self.text_switch(False)
        elif kind == "deleted":
            self.deleted -= 1
        elif kind == "inserted":
            self.inserted -= 1
        elif kind == "fldSimple":
            for reading in self._here():
                reading.end()
            self._changed()
        elif kind in ("story", "branch"):
            for reading in self._here():
                reading.leave()
            self._changed()

    def on_text(self, text: str) -> None:
        if self.collecting is None:
            return
        if self.trimming is not None:
            self.trimming.append(text)
            self.trimmed += len(text)
            if self.trimmed <= 2 * FIELD_CODE_CHARS:
                return
            # Too long to hold: what follows streams (the code is past its limit anyway).
            text = "".join(self.trimming).lstrip(_XML_BLANKS)
            self.trimming = None
        self._dispatch(text)

    def _collect(self, attributes: list[str], *, code: bool) -> None:
        """Start reading an element's text as code (w:instrText) or run text (w:t). Without
        xml:space="preserve", Word trims the blanks around it: "DD" and " E" are DDE."""
        self.collecting = code
        self.trimming = None if "preserve" in _values(attributes, _XML_SPACE) else []
        self.trimmed = 0
        self.text_switch(True)

    def _dispatch(self, text: str) -> None:
        code = bool(self.collecting)
        for reading in self._here():
            reading.text(text, code=code)

    def on_namespace(self, prefix: str | None, uri: str | None) -> None:
        self.work += 1
        self.prefixes.setdefault(prefix or "", []).append(uri or "")

    def on_namespace_end(self, prefix: str | None) -> None:
        declared = self.prefixes.get(prefix or "")
        if declared:
            declared.pop()
        if not declared:
            self.prefixes.pop(prefix or "", None)  # kept only while in scope

    def on_other(self, *_: object) -> None:
        """A comment, a processing instruction, a CDATA section: nothing to read, but each
        costs a call (counted, so a part made of them stays within the budget)."""
        self.work += 1

    def close(self) -> None:
        for reading in self.readings:
            reading.close()

    def _here(self) -> list[_Reading]:
        """The readings that see field content here (a deleted run isn't there once changes
        are accepted). They split at the first field content inside a tracked change: until
        then all three would read the same."""
        self._content()
        if self.deleted or self.inserted:
            if len(self.readings) == 1:
                first = self.readings[0]
                self.readings = [first, first.clone("deleted"), first.clone("inserted")]
            here = [
                reading
                for reading in self.readings
                if not (reading.skips == "deleted" and self.deleted)
                and not (reading.skips == "inserted" and self.inserted)
            ]
        else:
            here = self.readings
        self.work += FIELD_WORK * len(here)
        return here

    def _charge(self, units: int) -> None:
        self.work += units

    def _changed(self) -> None:
        self.live = any(reading.in_code() for reading in self.readings)

    def _content(self) -> None:
        if self.ignored:
            self.report("structure")  # field content Word may skip unread

    def _kind(self, tag: str) -> str:
        """What an element is to the reading: a kind of _WORD_ELEMENTS, "hidden" (in a
        namespace declared ignorable) or "" (nothing). Remembered per tag."""
        kind = _WORD_ELEMENTS.get(tag) or (
            "hidden" if tag.rpartition("}")[0] in self.ignorable_namespaces else ""
        )
        if len(self.kinds) > 2 * XML_NAMES:  # never, while the names stay within their bound
            self.kinds.clear()
        self.kinds[tag] = kind
        return kind

    def _declare_ignorable(self, attributes: list[str]) -> None:
        """mc:Ignorable: prefixes whose elements a consumer that doesn't understand their
        namespace skips (with their content), here and below. Only an attribute of that name
        counts, a scope is kept only when it adds a namespace (the set in effect before it is
        put back at its end, so each costs what it adds, counted as work), and at most
        IGNORABLE_DEPTH are open at once."""
        values = _values(attributes, _IGNORABLE_NAME)
        if not values:
            return  # the name was an attribute's value
        prefixes = " ".join(values).split()
        self.work += len(prefixes)
        declared = frozenset(
            self.prefixes[prefix][-1] for prefix in prefixes if self.prefixes.get(prefix)
        )
        if declared <= self.ignorable_namespaces:
            return
        if len(self.ignorable) >= IGNORABLE_DEPTH:
            raise _OverBudget
        self.ignorable.append((self.depth, self.ignorable_namespaces))
        self.ignorable_namespaces |= declared
        self.work += len(self.ignorable_namespaces) + IGNORABLE_SCOPE_WORK
        self.kinds.clear()


class _Refused(Exception):
    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule


class _OverBudget(Exception):
    pass


class _Unreadable(Exception):
    pass


@dataclass(slots=True)
class _Work:
    """Units of work left, shared by everything one upload's check does (UPLOAD_WORK)."""

    left: int


class _Budget:
    """What one check may still read (`bytes`, decompressed) and do (`work`, in units of about a
    microsecond). With a `pool`, every unit spent is taken from it too, and `work` is whichever
    is lower: the check's own cap or what is left for the whole upload."""

    __slots__ = ("_local", "_pool", "bytes")

    def __init__(self, bytes: int, work: int, pool: _Work | None = None) -> None:
        self.bytes = bytes
        self._local = work
        self._pool = pool

    @property
    def work(self) -> int:
        return self._local if self._pool is None else min(self._local, self._pool.left)

    @work.setter
    def work(self, value: int) -> None:
        spent = self.work - value
        self._local -= spent
        if self._pool is not None:
            self._pool.left -= spent

    def spend(self, units: int) -> None:
        """`units` of work: _OverBudget past the check's cap or the pool."""
        self.work -= units
        if self.work < 0:
            raise _OverBudget

    def read(self, size: int) -> None:
        """Bytes read and decoded: from `bytes`, and their cost from the pool only (the check's
        own work is what it parses and judges). _OverBudget past either."""
        self.bytes -= size
        if self._pool is not None:
            self._pool.left -= size // READ_BYTES_PER_WORK
            if self._pool.left < 0:
                raise _OverBudget
        if self.bytes < 0:
            raise _OverBudget


def _refuse(rule: str) -> NoReturn:
    raise _Refused(rule)


class _ExpatTarget(Protocol):
    """What _stream_story drives: expat's handlers for one part, plus what it costs (`work`)
    and the distinct names it used (`names`). _Story (Word fields) and _Formulas (spreadsheet
    formulas) both satisfy it."""

    names: set[str]
    work: int
    # Turns expat's character-data handler on and off (set by _stream_story): text is handed to
    # Python only while a field code or a formula is being read (review round 2, PARSERS2-2: expat
    # calls the handler once per line, so 256 MiB of newlines cost 7 s handed over unread).
    text_switch: Callable[[bool], None]

    def on_start(self, tag: str, attributes: list[str]) -> None: ...
    def on_end(self, tag: str) -> None: ...
    def on_text(self, text: str) -> None: ...
    def on_namespace(self, prefix: str | None, uri: str | None) -> None: ...
    def on_namespace_end(self, prefix: str | None) -> None: ...
    def on_other(self, *args: object) -> None: ...


def _decoded(part: IO[bytes], budget: _Budget | None = None) -> Iterator[str]:
    """The part's text, a chunk at a time, decoded as Office decodes an OPC part (ECMA-376 Part
    2, M1.17: UTF-8, or UTF-16 with its byte-order mark; a declared encoding must agree; no
    U+0000, which would let expat guess another encoding), so what is read is what Word reads:
    _Unreadable when it can't be. Its decompressed bytes are taken from `budget` (_OverBudget
    past it), and the upload's deadline is looked at once a chunk (_on_time): every loop that
    reads or parses a part goes through here."""
    decoder: codecs.IncrementalDecoder | None = None
    allowed: set[str] = set()
    head: str | None = ""  # the start, until the declaration is checked
    while True:
        _on_time()
        raw = part.read(CHUNK_BYTES)
        if budget is not None:
            budget.read(len(raw))
        if decoder is None:
            utf16 = raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
            allowed = {"utf-16"} if utf16 else {"utf-8"}
            decoder = codecs.getincrementaldecoder("utf-16" if utf16 else "utf-8-sig")()
        try:
            text = decoder.decode(raw, final=not raw)
        except UnicodeDecodeError:
            raise _Unreadable from None
        if "\x00" in text:
            raise _Unreadable  # never XML, and it would let expat guess another encoding
        if head is not None:
            head += text
            if len(head) < 512 and raw:
                continue
            if not _declaration_agrees(head, allowed):
                raise _Unreadable
            text, head = head, None
        if text:
            yield text
        if not raw:
            return


# What a part's text must contain for Word to find a field in it: the elements' local names
# (an element's name is never escaped: no character reference, no entity) or "<!" (a DTD that
# could declare entities, CDATA, a comment). A part without any is read, not parsed.
_FIELD_MARKUP = ("fldChar", "fldSimple", "nstrText", "<!")


def _mentions_fields(part: IO[bytes], budget: _Budget) -> bool:
    """Whether the part's text (_decoded, the whole of it, bytes taken from the budget) holds
    _FIELD_MARKUP."""
    found = False
    tail = ""
    for text in _decoded(part, budget):
        if not found:
            window = tail + text
            found = any(markup in window for markup in _FIELD_MARKUP)
            tail = window[-16:]
    return found


def _stream_story(part: IO[bytes], budget: _Budget, story: _ExpatTarget) -> bool:
    """Feed one part's text (_decoded) to `story` through defusedxml's expat parser (no DTD,
    entities or external references), a chunk at a time: memory stays bounded, whatever the
    part's size. False when it can't be read the way Word reads it (not well-formed, a DTD or
    entity, an encoding error); _OverBudget past the budget's work (what the story's reading
    cost is taken from it) or the XML's bounds (XML_TOKEN_BYTES: expat holds a start tag,
    an attribute value or a comment whole until it ends, so a part that is one huge tag
    stops there); _Refused from `story`'s report. Every PARSE_BYTES_PER_WORK bytes fed cost a unit
    of work, the parser PARSER_WORK, and character data reaches Python only while the story asks
    for it (text_switch)."""
    parser = defusedxml.ElementTree.DefusedXMLParser(
        target=story, forbid_dtd=True, forbid_entities=True, forbid_external=True
    )
    story.work += PARSER_WORK
    expat = parser.parser  # the handlers directly: no element objects, no name rewriting
    expat.StartElementHandler = story.on_start
    expat.EndElementHandler = story.on_end
    expat.CharacterDataHandler = None
    # ElementTree's parser hands whatever no other handler takes to its default handler, character
    # data too when its handler is off: none (DTDs and entities are refused by defusedxml's own
    # handlers, set above by the parser and untouched here).
    expat.DefaultHandlerExpand = None
    expat.DefaultHandler = None

    def switch(on: bool) -> None:
        expat.CharacterDataHandler = story.on_text if on else None

    story.text_switch = switch
    expat.StartNamespaceDeclHandler = story.on_namespace
    expat.EndNamespaceDeclHandler = story.on_namespace_end
    expat.CommentHandler = story.on_other
    expat.ProcessingInstructionHandler = story.on_other
    expat.StartCdataSectionHandler = story.on_other
    expat.EndCdataSectionHandler = story.on_other
    fed = 0  # bytes, as expat counts them (UTF-8)
    try:
        for text in _decoded(part):
            parser.feed(text)  # text: expat is told it is UTF-8, whatever the declaration says
            story.names.update(expat.intern)  # the names this chunk used, then forgotten
            expat.intern.clear()
            size = len(text) if text.isascii() else len(text.encode())
            fed += size
            story.work += size // PARSE_BYTES_PER_WORK
            if (
                story.work > budget.work
                or fed - expat.CurrentByteIndex > XML_TOKEN_BYTES
                or len(story.names) > XML_NAMES
            ):
                raise _OverBudget
        parser.close()
    except (_Unreadable, ParseError, DefusedXmlException):
        return False
    budget.work -= story.work
    return True


def _word_stories(
    package: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    budget: _PackageBudget | None = None,
) -> tuple[set[str], set[str]] | None:
    """The part names and extensions [Content_Types].xml declares as WordprocessingML XML
    (the main document, headers, footers, footnotes, endnotes, comments, the glossary, ...,
    under any name), or None if it can't be read (refused already by _active_content). What it
    declares is read once a package (_content_types)."""
    declared = _content_types(package, infos, budget or _PackageBudget())
    if declared is None:
        return None
    names: set[str] = set()
    extensions: set[str] = set()
    for kind, name, extension in declared:
        if not (kind.endswith("+xml") and ("wordprocessingml" in kind or "ms-word" in kind)):
            continue
        if name is not None:
            names.add(name)
        if extension is not None:
            extensions.add(extension)
    return names, extensions


def _word_fields(
    package: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    shared: _PackageBudget | None = None,
) -> bool:
    """A Word document's fields (R132): RejectedFile if one would start a program or fetch
    content from elsewhere, or can't be judged (FIELD_REFUSED), or the document is past the
    budget (TOO_LARGE_TO_CHECK); False if a part can't be read. Every part named *.xml and
    every part [Content_Types].xml declares WordprocessingML XML is a story, so none can hide
    under another name (more is read than holds fields: settings, styles, properties, custom
    XML, charts). Each is read whole (decoded, its bytes counted), and parsed again only if
    it mentions field markup (_FIELD_MARKUP): most parts don't, and reading costs a fraction
    of parsing; each member opened costs OPEN_WORK. The budget is the upload's
    (`shared.fields`): an embedded document spends it too (review round 1, PARSERS-6)."""
    shared = shared or _PackageBudget()
    budget = shared.fields
    try:
        stories = _word_stories(package, infos, shared)
        if stories is None:
            return False
        names, extensions = stories
        for info in infos:
            _on_time()
            name = unquote(info.filename).lower().lstrip("/")
            last = name.rpartition("/")[2]
            extension = last.rpartition(".")[2] if "." in last else ""
            if name == "[content_types].xml" or not (
                name.endswith(".xml") or name in names or extension in extensions
            ):
                continue
            if info.file_size > budget.bytes:
                raise _OverBudget  # refused unread
            budget.spend(OPEN_WORK)
            with package.open(info) as part:
                if not _mentions_fields(part, budget):
                    continue
            budget.spend(OPEN_WORK)
            with package.open(info) as part:  # its size is known: within the budget
                if not _stream_story(part, budget, _Story(_refuse)):
                    return False
    except _Unreadable:
        return False
    except _OverBudget:
        logger.info("attachment_field_check_refused", extra={"rule": "budget"})
        raise RejectedFile(TOO_LARGE_TO_CHECK) from None
    except _Refused as refused:
        logger.info("attachment_field_check_refused", extra={"rule": refused.rule})
        raise RejectedFile(FIELD_REFUSED) from None
    return True


# --- spreadsheet formulas that fetch or call out --------------------------------------------------
# Excel evaluates a cell formula when the workbook opens, and a few functions fetch content from
# elsewhere or call out as soon as they run (adversarial review of 73ff1e3, L8, R160): WEBSERVICE
# and the modern IMAGE fetch a URL; RTD calls a real-time-data COM server; CALL and REGISTER(.ID)
# bind and call a native DLL; EXEC is an Excel-4 (XLM) macro; the Google-Sheets IMPORT* functions
# fetch a URL. A workbook with external links, data connections, query tables or an XLM macro
# sheet is already refused by _active_content; this reads the formulas themselves (a cell's <f>,
# a defined name, a table's column formulas, data-validation and conditional-formatting formulas,
# a pivot cache's calculated fields), so a function name can't hide behind a character reference
# (WEB&#83;...), a comment, CDATA or a processing instruction, a namespace prefix on the element
# (<x:f>, review round 1, PARSERS-2) or a future-function prefix (_xlfn., _xlws.). HYPERLINK is
# kept: it follows its link only when someone clicks it (docs/activities.md#attachments). The same
# message serves the CSV check.
FORMULA_REFUSED = (
    "This file has a cell formula that fetches content from elsewhere or calls out (WEBSERVICE, "
    "IMAGE, RTD, CALL, REGISTER, EXEC or an IMPORT function). Remove it and upload the file again."
)
# The function names, as one alternation (letter case ignored, ASCII only, as Excel reads them).
_FETCHING_NAMES = (
    "WEBSERVICE|IMAGE|RTD|CALL|REGISTER(?:\\.ID)?|EXEC"
    "|IMPORTDATA|IMPORTXML|IMPORTHTML|IMPORTFEED|IMPORTRANGE"
)
_FETCHING_LIST = (
    "webservice", "image", "rtd", "call", "register", "register.id", "exec",
    "importdata", "importxml", "importhtml", "importfeed", "importrange",
)  # fmt: skip
# What a formula's text holds besides operators, references and names, removed before a name is
# looked for (review round 2, PARSERS2-5: "SUM('Purchase Register (2024-25)'!B2:B500)" and
# "Calls[[#This Row],[Call (min)]]*60" were refused): string literals ("..." with ""), quoted sheet
# or workbook names ('...' with ''), structured references ([...], one level nested, ' escaping).
# Each is replaced by a NUL, which no formula holds; an unclosed literal runs to the end.
_FORMULA_OPAQUE = re.compile(
    r'"(?:[^"]++|"")*+"'
    r"|'(?:[^']++|'')*+'"
    r"|\[(?:'.|[^\[\]']++|\[(?:'.|[^\[\]']++)*+\])*+\]",
    re.DOTALL,
)
# A fetching name as a token (not inside a longer name, MYIMAGE, IMAGELOOKUP, A1.IMAGE), with any
# future-function or eta prefix (_xlfn., _xlws., _xleta.), letter case ignored.
_FETCHING_TOKEN = re.compile(
    rf"(?<![\w.])(?ai:((?:_xlfn\.|_xlws\.|_xleta\.)*+)(?:{_FETCHING_NAMES}))(?![\w.])"
)
_NAME_NEEDLES = tuple(name.encode() for name in _FETCHING_LIST)
_CANDIDATE_NEEDLES = tuple(needle for needle in _NAME_NEEDLES if b"." not in needle)
_BRACKET_AFTER = re.compile(r"\(")
# Blanks in a formula, as Excel reads them: ASCII only (the pre-searches remove the same ones).
_FORMULA_BLANKS = " \t\r\n"
_BLANKS_BRACKET_AFTER = re.compile(r"[ \t\r\n]*+\(")
_ARGUMENT_END = re.compile(r"[ \t\r\n]*+[,;)]")  # what follows a function passed by name
# Review round 3, PARSERS3-3: a fetching function nobody would name a range after, passed as a
# value wherever an operand can stand and be called from: right after "(", an operator or "@", or
# at the start, and before ")", "," or ";" (MAP(WEBSERVICE,A1:A2), (WEBSERVICE), IF(1,(RTD),0),
# LAMBDA(...)(WEBSERVICE)); in a workbook formula also before its end, at any depth (a defined name
# "+WEBSERVICE" or "@WEBSERVICE", then called by its name). A CSV cell has no defined names, so
# there only inside a call: "- call, then email" is a note. IMAGE and REGISTER are left out: a
# range named "Image" or "Register" is ordinary (=SUM(Register)), so for them only the round-2
# shapes count (an "_xleta." prefix, an argument after "," or ";", a defined name that is only the
# function).
_VALUE_ONLY_NAMES = frozenset(
    {
        "webservice", "rtd", "call", "exec", "register.id",
        "importdata", "importxml", "importhtml", "importfeed", "importrange",
    }
)  # fmt: skip
_OPERAND_BEFORE = frozenset("(+-*/^&=<>@")  # or the formula's start
_VALUE_END = re.compile(r"[ \t\r\n]*+(?:[,;)]|\Z)")  # _ARGUMENT_END, or the formula's end
TOKEN_WORK = 2  # judging one fetching name found in a formula
FORMULA_WORK = 4  # judging one formula, whatever its length (Python's fixed cost: 3-4 us)


def _formula_calls(formula: str, *, blanks: bool, whole: bool) -> tuple[bool, int]:
    """Whether a formula (its text without the leading "=") fetches content or calls out, and how
    many fetching names were judged (each costs TOKEN_WORK). A fetching name counts when it is
    called (its "(" right after it, or after blanks when `blanks`), passed by name to another
    function (eta: an "_xleta." prefix, or an argument inside a call: a "," or ";" before it and a
    ",", ";" or ")" after it, review round 2, PARSERS2-6), or, when `whole`, the whole formula (a
    defined name that is only a fetching function); one of _VALUE_ONLY_NAMES also as an operand
    that can be called (review round 3, PARSERS3-3: after "(", an operator or nothing, before ")",
    "," or ";" inside a call, or, when `whole`, also before the end at any depth). A name right
    before "!" or "[" is a sheet or table (Register!B2,
    Image[Amount]); IMAGE or REGISTER only as an operand (=Image*2, =SUM(Register)), or any name
    among a note's words ("- email, call, visit"), is not a call. String literals, quoted names
    and structured references are removed first (_FORMULA_OPAQUE). Linear in the formula's length
    whatever it holds (review round 3, PARSERS3-1: the whole-formula test copied the text before
    every name, 0.5-0.8 s for one formula of 64K characters)."""
    lowered = formula.encode("ascii", "replace").lower()
    if not any(needle in lowered for needle in _NAME_NEEDLES):
        return False, 0
    text = _FORMULA_OPAQUE.sub("\x00", formula)
    quote = text.find('"')
    if quote >= 0:
        text = text[:quote]  # an unclosed string literal runs to the formula's end
    after_name = _BLANKS_BRACKET_AFTER if blanks else _BRACKET_AFTER
    first = len(text) - len(text.lstrip())  # where the text starts and ends, blanks aside: a
    last = len(text.rstrip())  # name spanning exactly that is the whole formula
    tokens = 0
    depth = 0
    counted = 0
    for match in _FETCHING_TOKEN.finditer(text):
        tokens += 1
        start, end = match.span()
        if "_xleta." in match[1].lower() or after_name.match(text, end):
            return True, tokens
        if whole and start == first and end == last:
            return True, tokens
        depth += text.count("(", counted, start) - text.count(")", counted, start)
        counted = start
        before = text[max(0, start - 256) : start].rstrip(_FORMULA_BLANKS)[-1:]
        if depth > 0 and before in (",", ";") and _ARGUMENT_END.match(text, end):
            return True, tokens
        if (
            match[0][len(match[1]) :].lower() in _VALUE_ONLY_NAMES
            and (not before or before in _OPERAND_BEFORE)
            and (whole or depth > 0)
            and (_VALUE_END if whole else _ARGUMENT_END).match(text, end)
        ):
            return True, tokens
    return False, tokens


# The formula scan's own caps (round 1, item A: ordinary large workbooks ran out of the Word
# fields' budget): SHEET_XML_BYTES of decompressed XML read and SHEET_XML_WORK units of work, both
# also drawn from the upload's pool (UPLOAD_WORK). A parsed element is 1 and 1 per attribute,
# PARSE_BYTES_PER_WORK bytes fed 1, a formula judged FORMULA_WORK plus 1 per FORMULA_CHARS_PER_WORK
# characters plus TOKEN_WORK a fetching name in it; reading costs the pool READ_BYTES_PER_WORK bytes
# a unit.
SHEET_XML_BYTES = 256 * 1024 * 1024
SHEET_XML_WORK = 1_000_000
FORMULA_CHARS = 64 * 1024  # of one formula (Excel holds 8,192)
FORMULA_CHARS_PER_WORK = 8
# The elements whose text is a formula, by local name (any namespace or prefix): a cell's <f>, a
# defined name, a table's calculated-column and totals-row formulas, a conditional format's
# <formula>, a data validation's <formula1>/<formula2> (x14 extensions write them as <xm:f>).
_FORMULA_ELEMENTS = frozenset(
    {
        "f",
        "definedName",
        "calculatedColumnFormula",
        "totalsRowFormula",
        "formula",
        "formula1",
        "formula2",
    }
)
# Elements whose "formula" attribute is a formula: a pivot cache's calculated field, a pivot
# table's calculated item.
_FORMULA_HOSTS = frozenset({"cacheField", "calculatedItem"})
# Content types (anywhere in the declared type, case ignored) of the parts that hold formulas.
_FORMULA_PART_TYPES = (
    "spreadsheetml.worksheet",
    "spreadsheetml.table",
    "spreadsheetml.dialogsheet",
    "spreadsheetml.chartsheet",
    "spreadsheetml.sheet.main",  # the workbook part: its defined names
    "spreadsheetml.template.main",  # a template's workbook part (.xltx; review round 2)
    "spreadsheetml.pivotcachedefinition",
    "spreadsheetml.pivottable",
    "drawingml.chart",  # series formulas (<c:f>)
)


class _Formulas:
    """expat's handlers for one spreadsheet part: the text of each formula element and the
    formula attributes, each judged once complete (_formula_calls). The same interface and
    bounds as _Story so _stream_story drives it: an element is 1 work and 1 per attribute,
    judging a formula 1 plus 1 per FORMULA_CHARS_PER_WORK characters plus TOKEN_WORK a fetching
    name; depth, attributes and distinct names are bounded (the XML itself). Text reaches it only
    inside a formula element, up to FORMULA_CHARS (text_switch). An element's local name is looked
    up once per distinct tag."""

    def __init__(self, report: Callable[[str], None]) -> None:
        self.report = report
        self.work = 0
        self.depth = 0
        self.names: set[str] = set()
        self.kinds: dict[str, str] = {}  # tag -> "formula", "host" or ""
        self.collecting = 0  # formula elements open (they do not nest in practice)
        self.parts: list[str] = []
        self.size = 0
        self.text_switch: Callable[[bool], None] = _no_switch

    def _kind(self, tag: str) -> str:
        kind = self.kinds.get(tag)
        if kind is None:
            local = tag.rpartition("}")[2]
            kind = (
                "formula"
                if local in _FORMULA_ELEMENTS
                else "host"
                if local in _FORMULA_HOSTS
                else ""
            )
            if len(self.kinds) <= 2 * XML_NAMES:
                self.kinds[tag] = kind
        return kind

    def on_start(self, tag: str, attributes: list[str]) -> None:
        self.depth += 1
        self.work += 1 + (len(attributes) >> 1)
        if len(attributes) > 2 * XML_ATTRIBUTES or self.depth > XML_DEPTH:
            raise _OverBudget
        kind = self._kind(tag)
        if kind == "formula":
            self.collecting += 1
            if self.collecting == 1:
                self.text_switch(True)
        elif kind == "host":
            for index in range(0, len(attributes) - 1, 2):
                if attributes[index].rpartition("}")[2] == "formula":
                    self._judge(attributes[index + 1][:FORMULA_CHARS])

    def on_end(self, tag: str) -> None:
        if self._kind(tag) == "formula":
            self.collecting -= 1
            if self.collecting == 0:
                self.text_switch(False)
                formula, self.parts, self.size = "".join(self.parts), [], 0
                self._judge(formula)
        self.depth -= 1

    def _judge(self, formula: str) -> None:
        _on_time()
        fetches, tokens = _formula_calls(formula, blanks=True, whole=True)
        self.work += FORMULA_WORK + len(formula) // FORMULA_CHARS_PER_WORK + TOKEN_WORK * tokens
        if fetches:
            self.report("formula")

    def on_text(self, text: str) -> None:
        if self.collecting and self.size < FORMULA_CHARS:
            chunk = text[: FORMULA_CHARS - self.size]
            self.parts.append(chunk)
            self.size += len(chunk)
            if self.size >= FORMULA_CHARS:
                self.text_switch(False)  # past what is judged: the rest is not handed over

    def on_namespace(self, prefix: str | None, uri: str | None) -> None:
        self.work += 1

    def on_namespace_end(self, prefix: str | None) -> None:
        pass

    def on_other(self, *_: object) -> None:
        self.work += 1


# Whether a part can hold a call at all (_may_fetch), from its text, before it is parsed: a call or
# an eta-reduced name needs a fetching name, then (blanks aside) its "(", or the "," ";" or ")"
# around an argument, or an "_xleta." prefix; XML can only separate or disguise those by a character
# reference ("&#"), a comment or CDATA ("<!") or a processing instruction ("<?" after the
# declaration). Searched as bytes (each character one byte, ASCII case folded, anything else "?"):
# the names first (a sheet of numbers or ordinary formulas holds none), then, only if one is there,
# the text with its blanks and "?" removed and ")", "," and ";" read as "(".
_SQUEEZE = bytes.maketrans(b"),;", b"(((")
# In the workbook part, a defined name can be only a fetching function, between its tags.
_SQUEEZE_WHOLE = bytes.maketrans(b"),;<", b"((((")
_SQUEEZED_OUT = b" \t\r\n?"
_CALL_SHAPES = (*(f"{name}(".encode() for name in _FETCHING_LIST), b"_xleta.")


def _may_fetch(part: IO[bytes], budget: _Budget, *, whole: bool = False) -> bool:
    """Whether the part's text (_decoded, read and paid for from `budget`) could hold a fetching
    call (see _SQUEEZE): "&#", "<!", or "<?" after the XML declaration; a fetching name in a call
    or eta shape; a fetching name at the end of a window with only blanks after it (its bracket may
    be in the next one); with `whole` (the workbook part), a fetching name that is a defined name's
    whole text. Plain byte searches, linear whatever the text holds."""
    found = False
    tail = b""
    first = True
    for text in _decoded(part, budget):
        if found:
            continue
        start = 0
        if first:
            declaration = _XML_DECLARATION.match(text)
            start = declaration.end() if declaration else 0
            first = False
        if any(text.find(markup, start) >= 0 for markup in ("&#", "<!", "<?")):
            found = True
            continue
        window = tail + text.encode("ascii", "replace").lower()
        tail = window[-64:]
        if not any(needle in window for needle in _NAME_NEEDLES):
            continue
        squeezed = window.translate(_SQUEEZE_WHOLE if whole else _SQUEEZE, _SQUEEZED_OUT)
        found = any(shape in squeezed for shape in _CALL_SHAPES) or window.rstrip(
            _SQUEEZED_OUT
        ).endswith(_NAME_NEEDLES)
    return found


def _xlsx_formula_parts(
    package: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    budget: _PackageBudget | None = None,
) -> tuple[set[str], set[str], set[str]] | None:
    """The part names and extensions [Content_Types].xml declares as a part that holds formulas
    (a worksheet, table, dialog or chart sheet, the workbook, a pivot cache or table, a chart), and
    the names of the workbook parts among them (its defined names), or None if it can't be read
    (refused already by _active_content). An Override names a part, a Default an extension (every
    part with it is read; where an Override says otherwise, the part is read anyway: reading more
    only costs time). What it declares is read once a package (_content_types)."""
    declared = _content_types(package, infos, budget or _PackageBudget())
    if declared is None:
        return None
    names: set[str] = set()
    extensions: set[str] = set()
    mains: set[str] = set()
    for kind, name, extension in declared:
        if not (kind.endswith("+xml") and any(t in kind for t in _FORMULA_PART_TYPES)):
            continue
        if name is not None:
            names.add(name)
            if kind.endswith(".main+xml"):
                mains.add(name)
        if extension is not None:
            extensions.add(extension)
    return names, extensions, mains


def _xlsx_formulas(
    package: zipfile.ZipFile,
    infos: list[zipfile.ZipInfo],
    shared: _PackageBudget | None = None,
) -> bool:
    """A workbook's formulas (L8, R160): RejectedFile if one fetches or calls out
    (FORMULA_REFUSED) or the workbook is past the budget (TOO_LARGE_TO_CHECK); False if a part
    can't be read. Every part [Content_Types].xml declares as a formula-bearing type is read,
    under any name: the parts Excel loads and evaluates, which it binds by their declared type,
    not their path (a part Excel never loads as a sheet, like an undeclared macro sheet, runs
    nothing, so it is not read). Each is read whole (decoded) and parsed only if it could hold a
    fetching call at all (_may_fetch): a sheet of numbers, or of ordinary formulas, is read but
    never parsed, so a large ordinary workbook stays cheap (review round 1, item A). The budget is
    the formula scan's own, the upload's (`shared.formulas`): an embedded workbook spends it too
    (PARSERS-6). Each member opened costs OPEN_WORK."""
    shared = shared or _PackageBudget()
    budget = shared.formulas  # its own caps, and the upload's pool
    try:
        parts = _xlsx_formula_parts(package, infos, shared)
        if parts is None:
            return False
        names, extensions, mains = parts
        for info in infos:
            _on_time()
            name = unquote(info.filename).lower().lstrip("/")
            last = name.rpartition("/")[2]
            extension = last.rpartition(".")[2] if "." in last else ""
            if name == "[content_types].xml" or not (name in names or extension in extensions):
                continue
            if info.file_size > budget.bytes:
                raise _OverBudget  # refused unread
            budget.spend(OPEN_WORK)
            with package.open(info) as part:
                if not _may_fetch(part, budget, whole=name in mains):
                    continue
            budget.spend(OPEN_WORK)
            with package.open(info) as part:  # its size is known: within the budget
                if not _stream_story(part, budget, _Formulas(_refuse)):
                    return False
    except _Unreadable:
        return False
    except _OverBudget:
        logger.info("attachment_formula_check_refused", extra={"rule": "budget"})
        raise RejectedFile(TOO_LARGE_TO_CHECK) from None
    except _Refused as refused:
        logger.info("attachment_formula_check_refused", extra={"rule": refused.rule})
        raise RejectedFile(FORMULA_REFUSED) from None
    return True


def _utf8_text(head: bytes, file: IO[bytes]) -> bool:
    """UTF-8 (a BOM allowed) without NUL bytes, checked across the whole file."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    file.seek(0)
    for chunk in iter(lambda: file.read(CHUNK_BYTES), b""):
        _on_time()
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


# --- CSV ------------------------------------------------------------------------------------------
# Excel opens a .csv by evaluating every cell that starts with "=", "+", "-" or "@" as a formula,
# and a formula of DDE's shape, app|topic!item (=cmd|' /c calc'!A0), asks to start that program
# (R132; docs/activities.md#attachments). Such a cell is refused. A file is read as cells
# separated by a comma, a semicolon (Excel's list separator in many regions), a tab, or whatever
# a first "sep=" line names: a cell any of them yields counts. Fields are read as Python's csv
# module reads them (a field starting with a quote runs to the closing quote, "" inside is a
# quote, text after the closing quote joins the field), by regular expressions over a stream:
# every field passes through C, never a Python loop per field or character, and memory holds
# one chunk and one field's first CSV_FIELD_CHARS. Excel holds at most 32,767 characters in a
# cell and 8,192 in a formula, so a field's first CSV_FIELD_CHARS decide; a longer field that
# starts (after blanks) like a formula, or is nothing but blanks that far, is refused (fail
# closed). Plain .txt isn't checked: double-clicking it opens a text editor, Excel reads one
# only through its import wizard, and prose has no cells to tell a formula from a sentence.
CSV_DDE_REFUSED = (
    "This file has a cell that Excel would run as a command (a formula with | and !, such as "
    "=cmd|' /c calc'!A0). Remove it and upload the file again."
)
# A CSV cell whose formula fetches content from elsewhere or calls out (L8, R160): the same
# functions and the same judge as a workbook's (_formula_calls, FORMULA_REFUSED), in a cell Excel
# would evaluate, called the way Excel calls a function: the name right before its "(" (no blank
# between: review round 1, PARSERS-3, "-Call (follow-up) with lab manager" is a note), or passed
# by name to another function (PARSERS2-6); outside string literals, quoted names and structured
# references (PARSERS2-5).
CSV_FORMULA_REFUSED = FORMULA_REFUSED
# Only a file whose bytes, quotes and blanks removed and ")" "," ";" read as "(", hold a fetching
# name in such a shape (or "_xleta.") is read cell by cell (_csv_mentions_call: plain byte
# searches, linear). Then each separator's reading walks the fields with the plain field
# expression only to know where each begins, and judges (in Python, the field read as the csv
# module reads it) only a field that holds a fetching name, or a quoted formula field with text
# after its closing quote (a name the quote splits). The judging is paid for, in units of about a
# microsecond across every reading of a file: CSV_PLAIN_FIELD_WORK a field that can't be a formula
# (its first character, or the first after an opening quote, isn't = + - @; review round 2,
# PARSERS2-7: a call log with "Call(s)" on every row ran out at 20,000 rows), CSV_FORMULA_FIELD_WORK
# a formula field plus 1 per CSV_JUDGE_CHARS_PER_WORK characters plus TOKEN_WORK a fetching name in
# it; past CSV_CALL_WORK the file is "too large to check". Candidates are found in bytes, one a
# character (ASCII case folded, anything else "?"), kept as the text arrives, never lower-cased
# again (PARSERS2-1: str.translate cost 80-110 ns a character on non-ASCII text, 3-8 s a file).
CSV_CALL_WORK = 1_000_000
CSV_PLAIN_FIELD_WORK = 2
CSV_FORMULA_FIELD_WORK = 12
CSV_JUDGE_CHARS_PER_WORK = 8
_CSV_SQUEEZED_OUT = b'" \t\r\n'
_CSV_QUOTED = re.compile(r'"((?:[^"]++|"")*+)"')
CSV_DELIMITERS = (",", ";", "\t")
CSV_FIELD_CHARS = 128 * 1024
_FORMULA_STARTS = ("=", "+", "-", "@")
_CSV_QUOTED_INSIDE = re.compile(r'(?:[^"]|"")*+')  # a quoted field's content, "" and all


@dataclass(frozen=True, slots=True)
class _CsvReading:
    """The expressions for one separator. A formula's parts: outside its string literals
    ("...") and quoted names ('...') anything but a "|"; DDE's shape is a "|" there, then a "!"
    in the same field. Raw text inside a quoted field doubles its quotes ("" is ")."""

    # Every field and its separator, up to a DDE field, an incomplete one or the end.
    walk: re.Pattern[str]
    dde: re.Pattern[str]  # a field (at its start) with DDE's shape
    # Complete fields and their separators, nothing judged, up to a quoted formula field with
    # text after its closing quote (`continued`), an incomplete field or the end.
    fields: re.Pattern[str]
    terminated: re.Pattern[str]  # one field and its separator
    continued: re.Pattern[str]  # a quoted formula field with text after its closing quote
    formula: re.Pattern[str]  # a field (at its start) the csv module reads as a formula
    field_end: re.Pattern[str]  # a separator
    separator: str

    @classmethod
    def of(cls, separator: str) -> _CsvReading:
        s = re.escape(separator)
        blank = rf"[^\S\r\n{s}]"
        plain_part = rf"""(?:[^{s}\r\n"'|]++|"[^"{s}\r\n]*+"|'[^'{s}\r\n]*+')"""
        quoted_part = r"""(?:[^"'|]++|""[^"]*+""|'(?:[^'"]|"")*+')"""
        after_plain = rf"[^{s}\r\n!]*+!"
        after_quoted = rf'(?:[^"!]|"")*+(?:!|"(?!")[^{s}\r\n!]*+!)'
        # Where a quoted field closes (a lone quote), the formula goes on in the text after it,
        # in the state it was in: outside, in a string literal, in a quoted name.
        closes = (
            r'(?:"(?!")'
            rf'|""[^"]*+"(?!")[^"{s}\r\n]*+"'
            rf"|'(?:[^'\"]|\"\")*+\"(?!\")[^'{s}\r\n]*+')"
        )
        dde = (
            rf"{blank}*+[=+\-@]{plain_part}*+\|{after_plain}"
            rf'|"\s*+[=+\-@]{quoted_part}*+(?:\|{after_quoted}'
            rf"|{closes}{plain_part}*+\|{after_plain})"
            rf'|"\s*+"(?!"){blank}*+[=+\-@]{plain_part}*+\|{after_plain}'
        )
        field = rf'(?:"(?:[^"]++|"")*+"[^{s}\r\n]*+|(?:[^{s}\r\n"][^{s}\r\n]*+)?)'
        # A quoted formula field that closes early with more text after it (malformed CSV that
        # Python's csv module, like Excel, joins to the field, so a name could be split across
        # the quote): judged whatever names its bytes hold.
        continued = rf'"\s*+[=+\-@](?:[^"]++|"")*+"(?!"){blank}*+[^\s{s}]'
        return cls(
            walk=re.compile(rf"(?:(?!{dde}){field}(?:{s}|\r\n?|\n))*+"),
            dde=re.compile(dde),
            fields=re.compile(rf"(?:(?!{continued}){field}(?:{s}|\r\n?|\n))*+"),
            terminated=re.compile(rf"{field}(?:{s}|\r\n?|\n)"),
            continued=re.compile(continued),
            formula=re.compile(rf'{blank}*+[=+\-@]|"\s*+[=+\-@]|"\s*+"(?!"){blank}*+[=+\-@]'),
            field_end=re.compile(rf"[{s}\r\n]"),
            separator=separator,
        )


def _csv_text(head: bytes, file: IO[bytes]) -> bool:
    """UTF-8 text (_utf8_text) without a cell Excel would run as a DDE formula
    (CSV_DDE_REFUSED) or a formula that fetches content or calls out (CSV_FORMULA_REFUSED)."""
    if not _utf8_text(head, file):
        return False
    if _csv_dde(file):
        logger.info("attachment_field_check_refused", extra={"rule": "csv_dde"})
        raise RejectedFile(CSV_DDE_REFUSED)
    if _csv_formula_calls(file):
        logger.info("attachment_field_check_refused", extra={"rule": "csv_formula"})
        raise RejectedFile(CSV_FORMULA_REFUSED)
    return True


def _csv_dde(file: IO[bytes]) -> bool:
    """Whether a cell, by any of the separators, is a DDE formula. One read finds which of "|",
    "!" and the separators the file holds: without a "|" and a "!" there is no DDE formula and
    nothing more is read; a separator it doesn't hold gives whole lines as fields, the same for
    each, so that reading is done once. Each reading streams the file once (_csv_reading_finds),
    so the cost is linear in the upload's size (ATTACHMENT_MAX_BYTES)."""
    for reading in _csv_readings(file, "|", "!"):
        if _csv_reading_finds(file, reading, reading.walk, reading.dde):
            return True
    return False


def _csv_formula_calls(file: IO[bytes]) -> bool:
    """Whether a cell, by any of the separators, is a formula that fetches or calls out. Without
    a fetching name in a call or eta shape in the file (_csv_mentions_call) there is nothing to
    find and nothing more is read; otherwise, like _csv_dde, each separator's reading streams the
    file once (_csv_calls_in), judging only the fields worth it, within CSV_CALL_WORK
    (RejectedFile, TOO_LARGE_TO_CHECK, past it)."""
    if not _csv_mentions_call(file):
        return False
    budget = _Budget(0, CSV_CALL_WORK)
    try:
        return any(
            _csv_calls_in(file, reading, budget)
            for reading in _csv_readings(file, "(", require_any=_FORMULA_STARTS)
        )
    except _OverBudget:
        logger.info("attachment_field_check_refused", extra={"rule": "csv_formula_budget"})
        raise RejectedFile(TOO_LARGE_TO_CHECK) from None


def _csv_mentions_call(file: IO[bytes]) -> bool:
    """Whether the file's bytes, quotes and blanks removed (a quote can split a name, blanks can
    separate it from its bracket), ASCII case folded and ")" "," ";" read as "(", hold a fetching
    name followed by "(" (a call, or a name passed as an argument) or "_xleta.": plain byte
    searches a chunk, linear whatever the file holds."""
    file.seek(0)
    tail = b""
    for chunk in iter(lambda: file.read(CHUNK_BYTES), b""):
        _on_time()
        window = tail + chunk.lower().translate(_SQUEEZE, _CSV_SQUEEZED_OUT)
        if any(shape in window for shape in _CALL_SHAPES):
            return True
        tail = window[-16:]
    return False


def _csv_readings(
    file: IO[bytes], *required: str, require_any: tuple[str, ...] = ()
) -> list[_CsvReading]:
    """The readings to run: one per separator the file holds, plus one for whole lines. Empty
    when a `required` character is absent, or when none of `require_any` is present (then there
    is nothing of the shape to find and the file is not read again)."""
    file.seek(0)
    first = file.readline(64).decode("utf-8", "replace").removeprefix(chr(0xFEFF))
    sep = first[4] if first.startswith("sep=") and first[4:5] not in ("", "\r", "\n", '"') else ""
    candidates = list(dict.fromkeys(d for d in (*CSV_DELIMITERS, sep) if d))
    wanted = {char: char.encode() for char in (*required, *require_any, *candidates)}
    present: set[str] = set()
    file.seek(0)
    tail = b""
    for chunk in iter(lambda: file.read(CHUNK_BYTES), b""):
        _on_time()
        window = tail + chunk
        present.update(char for char, encoded in wanted.items() if encoded in window)
        tail = window[-4:]
    if not set(required) <= present or (require_any and not set(require_any) & present):
        return []
    separators = [d for d in candidates if d in present]
    separators += [d for d in candidates if d not in present][:1]  # whole lines, once
    return [_CsvReading.of(d) for d in separators]


class _CallCandidates:
    """Where, in a buffer of CSV text, the fields that hold a fetching name are: each name's next
    place in `lowered` (the buffer as bytes, one a character, ASCII case folded), searched in C
    and kept in a heap until passed, so a buffer is searched once per occurrence."""

    def __init__(self, lowered: bytes) -> None:
        self.lowered = lowered
        self.heap: list[tuple[int, bytes]] = []
        for needle in _CANDIDATE_NEEDLES:
            found = lowered.find(needle)
            if found >= 0:
                self.heap.append((found, needle))
        heapq.heapify(self.heap)

    def first(self, at: int) -> int:
        """The first candidate at or after `at`, or -1."""
        heap = self.heap
        while heap and heap[0][0] < at:
            needle = heapq.heappop(heap)[1]
            found = self.lowered.find(needle, at)
            if found >= 0:
                heapq.heappush(heap, (found, needle))
        return heap[0][0] if heap else -1


def _csv_formula(field: str) -> str | None:
    """The formula a CSV field holds as the csv module (and Excel) reads it (a quoted field's
    content with "" as ", any text after its closing quote joined to it), without the leading
    blanks and the = + - @ that make it one; None if it is no formula."""
    if field.startswith('"'):
        closed = _CSV_QUOTED.match(field)
        if closed is None:
            value = field[1:].replace('""', '"')
        else:
            value = closed[1].replace('""', '"') + field[closed.end() :]
    else:
        value = field
    body = value.lstrip()
    if not body or body[0] not in _FORMULA_STARTS:
        return None
    return body[1:]


def _csv_field_calls(field: str, separator: str, budget: _Budget) -> bool:
    """Whether one field (with its separator, if any) is a formula that fetches or calls out
    (_formula_calls, no blank before a bracket), paid for from `budget` (_OverBudget past it)."""
    _on_time()
    if field.endswith("\r\n"):
        field = field[:-2]
    elif field[-1:] in (separator, "\r", "\n"):
        field = field[:-1]
    formula = _csv_formula(field)
    if formula is None:
        budget.work -= CSV_PLAIN_FIELD_WORK
    else:
        budget.work -= CSV_FORMULA_FIELD_WORK + len(field) // CSV_JUDGE_CHARS_PER_WORK
    if budget.work < 0:
        raise _OverBudget
    if formula is None:
        return False
    fetches, tokens = _formula_calls(formula, blanks=False, whole=False)
    budget.work -= TOKEN_WORK * tokens
    return fetches


def _csv_calls_in(file: IO[bytes], reading: _CsvReading, budget: _Budget) -> bool:
    """One reading of the file (UTF-8, a BOM dropped), a chunk at a time: whether a field calls a
    fetching function (_csv_field_calls). The plain field expression walks the fields only to find
    where the next one worth judging begins: the one holding the next candidate (_CallCandidates),
    or a quoted formula field with text after its closing quote; that field alone is judged. The
    field at the end of a chunk is kept until the next one; a field longer than CSV_FIELD_CHARS is
    judged on what was read (Excel evaluates no formula over 8,192 characters: review round 1,
    PARSERS-7) and skipped to its end. The text's bytes for the candidates are made once, as each
    chunk arrives, and cut with the text."""
    file.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    buffer = ""
    lowered = b""
    skipping = ""  # "": reading fields; '"': inside a long quoted field; "plain": to its end
    final = False
    while not final:
        _on_time()
        raw = file.read(CHUNK_BYTES)
        final = not raw
        text = decoder.decode(raw, final=final)
        buffer += text
        lowered += text.encode("ascii", "replace").lower()
        buffer, skipping = _skipped(buffer, skipping, reading)
        lowered = lowered[len(lowered) - len(buffer) :]
        if skipping:
            continue  # still inside a long field
        candidates = _CallCandidates(lowered)
        at = 0
        while True:
            candidate = candidates.first(at)
            limit = len(buffer) if candidate < 0 else candidate
            start = _matched_range(reading.fields, buffer, at, limit)
            if candidate < 0 and not reading.continued.match(buffer, start):
                at = start  # what is left is the last field, incomplete, nothing to judge yet
                break
            whole = reading.terminated.match(buffer, start)
            if whole is None and not final and len(buffer) - start <= CSV_FIELD_CHARS:
                at = start  # the field goes on in the next chunk: judge it then
                break
            end = whole.end() if whole is not None else len(buffer)
            if not reading.formula.match(buffer, start):
                budget.work -= CSV_PLAIN_FIELD_WORK  # no formula: nothing in it to judge
                if budget.work < 0:
                    raise _OverBudget
            elif _csv_field_calls(buffer[start:end], reading.separator, budget):
                return True
            if whole is None and not final:  # longer than CSV_FIELD_CHARS: judged, now skipped
                quoted = buffer.startswith('"', start)
                at, skipping = (start + 1, '"') if quoted else (start, "plain")
                break
            at = end
        buffer, lowered = buffer[at:], lowered[at:]
        if not skipping and not final and len(buffer) > CSV_FIELD_CHARS:
            # The last field, longer than Excel holds a formula, with no candidate in what was
            # read: skipped to its end.
            skipping = '"' if buffer.startswith('"') else "plain"
            if skipping == '"':
                buffer, lowered = buffer[1:], lowered[1:]
        buffer, skipping = _skipped(buffer, skipping, reading)
        lowered = lowered[len(lowered) - len(buffer) :]
    return False


def _skipped(buffer: str, skipping: str, reading: _CsvReading) -> tuple[str, str]:
    """`buffer` with the rest of a long field being skipped removed (inside its quotes, "" and
    all, then to its separator), and what is still being skipped ("" once its end is found)."""
    if skipping == '"':
        inside = _matched(_CSV_QUOTED_INSIDE, buffer)
        if inside >= len(buffer) - 1:  # a quote at the end may be the first of ""
            return buffer[inside:], skipping
        buffer, skipping = buffer[inside + 1 :], "plain"
    if skipping == "plain":
        found = reading.field_end.search(buffer)
        if found is None:
            return "", skipping
        buffer, skipping = buffer[found.end() :], ""
    return buffer, skipping


def _matched_range(pattern: re.Pattern[str], text: str, start: int, end: int) -> int:
    """Where `pattern` (which can match nothing) stops matching from `start`, not past `end`."""
    found = pattern.match(text, start, end)
    return found.end() if found else start


def _csv_reading_finds(
    file: IO[bytes], reading: _CsvReading, walk: re.Pattern[str], danger: re.Pattern[str]
) -> bool:
    """One reading of the file (UTF-8, a BOM dropped), a chunk at a time: whether a field matches
    `danger` (DDE's shape), `walk` skipping the fields that do not. What isn't decided yet is the
    field at the end of the chunk, kept until the next one; past CSV_FIELD_CHARS it is decided
    (refused if it starts like a formula or is all blanks, else skipped to its end without being
    kept)."""
    file.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    buffer = ""
    skipping = ""  # "": reading fields; '"': inside a long quoted field; "plain": to its end
    for raw in iter(lambda: file.read(CHUNK_BYTES), b""):
        _on_time()
        buffer += decoder.decode(raw)
        while True:
            if skipping == '"':
                inside = _matched(_CSV_QUOTED_INSIDE, buffer)
                if inside >= len(buffer) - 1:  # a quote at the end may be the first of ""
                    buffer = buffer[inside:]
                    break
                buffer, skipping = buffer[inside + 1 :], "plain"
            if skipping == "plain":
                found = reading.field_end.search(buffer)
                if found is None:
                    buffer = ""
                    break
                buffer, skipping = buffer[found.end() :], ""
            position = _matched(walk, buffer)
            if danger.match(buffer, position):
                return True
            buffer = buffer[position:]
            if len(buffer) <= CSV_FIELD_CHARS:
                break
            content = buffer[1:] if buffer.startswith('"') else buffer
            start = content[:CSV_FIELD_CHARS].lstrip()
            if not start or start.startswith(_FORMULA_STARTS):
                return True  # a formula longer than Excel holds, or blanks: fail closed
            skipping = '"' if buffer.startswith('"') else "plain"
            buffer = buffer[1:] if skipping == '"' else buffer
    buffer += decoder.decode(b"", final=True)
    return skipping == "" and danger.match(buffer) is not None


def _matched(pattern: re.Pattern[str], text: str) -> int:
    """Where `pattern` (which can match nothing) stops matching at the start of `text`."""
    found = pattern.match(text)
    return found.end() if found else 0


def _dde_cell(cell: str) -> bool:
    """A cell (its raw CSV field, separated by commas) that Excel evaluates and that has DDE's
    shape. "-5", "@name", =SUM(A1:A3) or =A1&"|"&B1 are not."""
    return _CsvReading.of(",").dde.match(cell) is not None


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
        _ooxml("word/document.xml", _word_fields),
    ),
    "xlsx": FileKind(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        _ooxml("xl/workbook.xml", _xlsx_formulas),
    ),
    "pptx": FileKind(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        _ooxml("ppt/presentation.xml"),
    ),
    "csv": FileKind("text/csv", _bounded(_csv_text)),
    "txt": FileKind("text/plain", _bounded(_utf8_text)),
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
