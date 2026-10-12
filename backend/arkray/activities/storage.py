"""Attachment files: what is accepted, how it is received, where it is kept, how it is
scanned (docs/activities.md#attachments).

- **Types**: an explicit catalog of business documents and images the server can recognise
  by their *content*; ATTACHMENT_ALLOWED_EXTENSIONS enables a subset (a system check refuses
  anything outside the catalog). Executables, scripts, HTML, SVG, archives and everything
  else are refused, whatever their name or declared type. A file must be what its extension
  says: a PDF starts like a PDF, an image is that image format, Office files are the right
  OOXML package (without macros, ActiveX, OLE objects or anything fetched from elsewhere when
  opened: its XML parsed, never searched as bytes; a Word document also without a field that
  starts a program or fetches content from elsewhere), text is UTF-8 without NUL bytes (a CSV
  also without a cell Excel would run as a DDE formula).
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
from typing import IO, Any, NoReturn, Protocol, cast
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


def _ooxml(
    main_part: str,
    body: Callable[[zipfile.ZipFile, list[zipfile.ZipInfo]], bool] | None = None,
) -> Callable[[bytes, IO[bytes]], bool]:
    """An Office Open XML package with its main part and no active content: no macro project,
    OLE object or ActiveX control (by part name, embedded file type, declared content type,
    relationship type), nothing fetched from elsewhere when it opens (a remote template, a
    linked picture or object, a link to another workbook, a data connection). The zip's
    central directory, then only its small XML parts ([Content_Types].xml, relationships;
    each and in total bounded) are read, so a zip bomb costs little here (enhancement security
    review: a renamed VBA part, an OLE package wrapping an executable, ActiveX all passed a
    name-only check). Then `body`, if any, reads the content itself (a Word document's fields:
    _word_fields), bounded too, and may refuse with a message of its own (RejectedFile).
    Anything that can't be read or parsed is refused (fail closed)."""

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
                if _active_content(package, infos):
                    return False
                return body is None or body(package, infos)
        except RejectedFile:
            raise  # refused with its own message (a Word field, a document too large to check)
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
    if not _declaration_agrees(text, allowed):
        return None
    try:
        return defusedxml.ElementTree.fromstring(text, forbid_dtd=True, forbid_entities=True)
    except (ParseError, DefusedXmlException, ValueError):
        return None


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
# decoded as _parse_part decodes; one whose text mentions field markup (_FIELD_MARKUP) is then
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
#   FIELD_CODE_CHARS_PER_WORK characters, per reading. A unit of a real document costs
#   under a microsecond at full clock, and, measured, no shape of part costs more per unit (one
#   of nothing but empty elements, a zip bomb's shape: 0.4 of it), so 1 million bound parsing to
#   under a second of CPU, what an authenticated user can make a web worker spend per upload
#   (measured worst: 0.7 s, a 63 MiB document read whole and parsed to the budget); a real
#   document with fields reaches it at some 15 MiB of XML (about 700 pages). What expat itself
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
class _Budget:
    bytes: int
    work: int


def _refuse(rule: str) -> NoReturn:
    raise _Refused(rule)


def _decoded(part: IO[bytes], budget: _Budget | None = None) -> Iterator[str]:
    """The part's text, a chunk at a time, decoded as _parse_part decodes (UTF-8, or UTF-16
    with its byte-order mark; a declared encoding must agree; no U+0000), so what is read is
    what Word reads: _Unreadable when it can't be. Its decompressed bytes are taken from
    `budget` (_OverBudget past it)."""
    decoder: codecs.IncrementalDecoder | None = None
    allowed: set[str] = set()
    head: str | None = ""  # the start, until the declaration is checked
    while True:
        raw = part.read(CHUNK_BYTES)
        if budget is not None:
            budget.bytes -= len(raw)
            if budget.bytes < 0:
                raise _OverBudget
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


def _stream_story(part: IO[bytes], budget: _Budget, story: _Story) -> bool:
    """Feed one part's text (_decoded) to `story` through defusedxml's expat parser (no DTD,
    entities or external references), a chunk at a time: memory stays bounded, whatever the
    part's size. False when it can't be read the way Word reads it (not well-formed, a DTD or
    entity, an encoding error); _OverBudget past the budget's work (what the story's reading
    cost is taken from it) or the XML's bounds (XML_TOKEN_BYTES: expat holds a start tag,
    an attribute value or a comment whole until it ends, so a part that is one huge tag
    stops there); _Refused from `story`'s report."""
    parser = defusedxml.ElementTree.DefusedXMLParser(
        target=story, forbid_dtd=True, forbid_entities=True, forbid_external=True
    )
    expat = parser.parser  # the handlers directly: no element objects, no name rewriting
    expat.StartElementHandler = story.on_start
    expat.EndElementHandler = story.on_end
    expat.CharacterDataHandler = story.on_text
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
            fed += len(text) if text.isascii() else len(text.encode())
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
    package: zipfile.ZipFile, infos: list[zipfile.ZipInfo]
) -> tuple[set[str], set[str]] | None:
    """The part names and extensions [Content_Types].xml declares as WordprocessingML XML
    (the main document, headers, footers, footnotes, endnotes, comments, the glossary, ...,
    under any name), or None if it can't be read (refused already by _active_content)."""
    names: set[str] = set()
    extensions: set[str] = set()
    for info in infos:
        if unquote(info.filename).lower().lstrip("/") != "[content_types].xml":
            continue
        with package.open(info) as part:
            raw = part.read(OOXML_XML_PART_BYTES + 1)
        root = _parse_part(raw) if len(raw) <= OOXML_XML_PART_BYTES else None
        if root is None:
            return None
        for element in root.iter():
            declared = {_local(key): value for key, value in element.attrib.items()}
            kind = declared.get("contenttype", "").partition(";")[0].strip().casefold()
            if not (kind.endswith("+xml") and ("wordprocessingml" in kind or "ms-word" in kind)):
                continue
            if "partname" in declared:
                names.add(unquote(declared["partname"]).strip().lower().lstrip("/"))
            if "extension" in declared:
                extensions.add(declared["extension"].strip().lower().lstrip("."))
    return names, extensions


def _word_fields(package: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> bool:
    """A Word document's fields (R132): RejectedFile if one would start a program or fetch
    content from elsewhere, or can't be judged (FIELD_REFUSED), or the document is past the
    budget (TOO_LARGE_TO_CHECK); False if a part can't be read. Every part named *.xml and
    every part [Content_Types].xml declares WordprocessingML XML is a story, so none can hide
    under another name (more is read than holds fields: settings, styles, properties, custom
    XML, charts). Each is read whole (decoded, its bytes counted), and parsed again only if
    it mentions field markup (_FIELD_MARKUP): most parts don't, and reading costs a fraction
    of parsing."""
    stories = _word_stories(package, infos)
    if stories is None:
        return False
    names, extensions = stories
    budget = _Budget(WORD_XML_BYTES, WORD_XML_WORK)
    try:
        for info in infos:
            name = unquote(info.filename).lower().lstrip("/")
            last = name.rpartition("/")[2]
            extension = last.rpartition(".")[2] if "." in last else ""
            if name == "[content_types].xml" or not (
                name.endswith(".xml") or name in names or extension in extensions
            ):
                continue
            if info.file_size > budget.bytes:
                raise _OverBudget  # refused unread
            with package.open(info) as part:
                if not _mentions_fields(part, budget):
                    continue
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
    field_end: re.Pattern[str]  # a separator

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
        field = rf'(?:"(?:[^"]|"")*+"[^{s}\r\n]*+|(?:[^{s}\r\n"][^{s}\r\n]*+)?)'
        return cls(
            walk=re.compile(rf"(?:(?!{dde}){field}(?:{s}|\r\n?|\n))*+"),
            dde=re.compile(dde),
            field_end=re.compile(rf"[{s}\r\n]"),
        )


def _csv_text(head: bytes, file: IO[bytes]) -> bool:
    """UTF-8 text (_utf8_text) without a cell Excel would run as a DDE formula
    (RejectedFile, CSV_DDE_REFUSED)."""
    if not _utf8_text(head, file):
        return False
    if _csv_dde(file):
        logger.info("attachment_field_check_refused", extra={"rule": "csv_dde"})
        raise RejectedFile(CSV_DDE_REFUSED)
    return True


def _csv_dde(file: IO[bytes]) -> bool:
    """Whether a cell, by any of the separators, is a DDE formula. One read finds which of "|",
    "!" and the separators the file holds: without a "|" and a "!" there is no DDE formula and
    nothing more is read; a separator it doesn't hold gives whole lines as fields, the same for
    each, so that reading is done once. Each reading streams the file once (_csv_reading_finds),
    so the cost is linear in the upload's size (ATTACHMENT_MAX_BYTES)."""
    file.seek(0)
    first = file.readline(64).decode("utf-8", "replace").removeprefix(chr(0xFEFF))
    sep = first[4] if first.startswith("sep=") and first[4:5] not in ("", "\r", "\n", '"') else ""
    candidates = list(dict.fromkeys(d for d in (*CSV_DELIMITERS, sep) if d))
    wanted = {char: char.encode() for char in ("|", "!", *candidates)}
    present: set[str] = set()
    file.seek(0)
    tail = b""
    for chunk in iter(lambda: file.read(CHUNK_BYTES), b""):
        window = tail + chunk
        present.update(char for char, encoded in wanted.items() if encoded in window)
        tail = window[-4:]
    if not {"|", "!"} <= present:
        return False
    separators = [d for d in candidates if d in present]
    separators += [d for d in candidates if d not in present][:1]  # whole lines, once
    return any(_csv_reading_finds(file, _CsvReading.of(d)) for d in separators)


def _csv_reading_finds(file: IO[bytes], reading: _CsvReading) -> bool:
    """One reading of the file (UTF-8, a BOM dropped), a chunk at a time: whether a field has
    DDE's shape. What isn't decided yet is the field at the end of the chunk, kept until the
    next one; past CSV_FIELD_CHARS it is decided (refused if it starts like a formula or is all
    blanks, else skipped to its end without being kept)."""
    file.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    buffer = ""
    skipping = ""  # "": reading fields; '"': inside a long quoted field; "plain": to its end
    for raw in iter(lambda: file.read(CHUNK_BYTES), b""):
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
            position = _matched(reading.walk, buffer)
            if reading.dde.match(buffer, position):
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
    return skipping == "" and reading.dde.match(buffer) is not None


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
        _ooxml("xl/workbook.xml"),
    ),
    "pptx": FileKind(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        _ooxml("ppt/presentation.xml"),
    ),
    "csv": FileKind("text/csv", _csv_text),
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
