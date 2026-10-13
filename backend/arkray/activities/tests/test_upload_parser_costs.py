"""What the upload checks cost (adversarial review of 73ff1e3, L7/L8, R159/R160, review rounds 2
and 3: PARSERS2-1, -2, -3, -7, PARSERS3-1, -2). The production backend has 2 CPUs and 8 sync
workers with a 30 s timeout, and a person may upload 30 files a minute, so an upload of at most
10 MB should be checked within about a second or two of CPU. The unit budgets are meant to hold
every shape to that, but each review round found a path they did not price; so these tests hold
every worst shape the reviews found to CPU_BOUND seconds of process CPU (about half of it at full
clock on the development laptop) and MEMORY_BOUND traced bytes, or to "too large to check" within
them, and a per-upload CPU deadline (UPLOAD_CPU_SECONDS, TestUploadDeadline) stops any path still
unpriced. The inputs are inert (nothing in them fetches anything) and built in memory."""

from __future__ import annotations

import functools
import io
import struct
import time
import tracemalloc
import warnings
import zipfile
from collections.abc import Callable, Sequence
from xml.sax.saxutils import quoteattr

import pytest

from arkray.activities import storage
from tests.cpu_scale import bound, slowdown

# Seconds of process CPU for one upload of at most 10 MB on the development laptop; every bound
# here is scaled to the speed of the process running it (tests/cpu_scale.py: coverage tracing
# the checks, a slower or loaded CI runner), never below what it is.
CPU_BOUND = 2.0
MEMORY_BOUND = 40 * 1024 * 1024  # traced bytes
LIMIT = 10 * 1024 * 1024
SM = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
WORKSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
DOCMAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TOO_LARGE = storage.TOO_LARGE_TO_CHECK


@pytest.fixture(autouse=True)
def deadline_at_this_speed(monkeypatch):
    """The upload's CPU deadline scaled like the bounds: an ordinary upload a slow, traced
    runner checks in more than the laptop's seconds must still pass (the deadline's own tests
    set UPLOAD_CPU_SECONDS themselves)."""
    monkeypatch.setattr(storage, "UPLOAD_CPU_SECONDS", storage.UPLOAD_CPU_SECONDS * slowdown())


def check(ext: str, content: bytes) -> tuple[object, float]:
    """The catalog check (True, False or the refusal message) and the CPU it took."""
    assert len(content) <= LIMIT, len(content)
    started = time.process_time()
    try:
        outcome: object = storage.CATALOG[ext].recognise(content[:8192], io.BytesIO(content))
    except storage.RejectedFile as refused:
        outcome = str(refused)
    return outcome, time.process_time() - started


def peak(ext: str, content: bytes) -> int:
    tracemalloc.start()
    try:
        check(ext, content)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def fill(unit: str, budget: int) -> str:
    return unit * (budget // len(unit.encode()))


# --- CSV (PARSERS2-1, PARSERS2-7) -------------------------------------------------------------
ROW = (
    "José Fernández,Fernández Diagnostics,+91 98200 00000,jose@example.invalid,2026-10-12,Pune,"
    '"Met lab head; demo next week"\n'
)
HEAD = (
    "Name,Company,Phone,Email,Date,City,Notes\n"
    "Asha,Asha Lab,+91 1,a@x.invalid,2026-10-12,Pune,Image(s) shared\n"
)
CSV_SHAPES = {
    # An ordinary export with accented names and one note that mentions "Image(s)": the one
    # mention makes every reading run (2.9 s before round 2: a translate() per chunk).
    "accented-export": HEAD + fill(ROW, LIMIT - len(HEAD.encode()) - 8),
    "accented-short-fields": "Name,City\nAsha,Image(s) attached\n"
    + fill("José Fernández,Mumbai;Lab\tvisit on 2026-10-12 follow up\n", LIMIT - 100),
    # Fields just under CSV_FIELD_CHARS, carried over many chunks, every separator present, and a
    # "|" and "!" so the DDE readings run too (5.4-8.3 s before).
    "hindi-long-fields": "sep=|\nnote image(s) 2026-10-12 a!b\n"
    + fill("क" * 125_000 + ",;\t|", LIMIT - 64),
    "accented-long-fields": "sep=|\nnote image(s) 2026-10-12 a!b\n"
    + fill("é" + "a" * 127_000 + ",;\t|", LIMIT - 64),
    "hindi-40k-fields": "note image(s) 2026-10-12\n" + fill("क" * 40_000 + ",;\t", LIMIT - 64),
    # A call log: "Call(s)" on every row (refused as too large to check at 20,000 rows before).
    "call-log": "Date,Lead,Activity,Notes\n"
    + fill(
        "2026-10-12,Asha Lab,Call(s),spoke to manager; will revisit next week about strips\n",
        LIMIT - 64,
    ),
}


class TestCsvCost:
    @pytest.mark.parametrize("label", sorted(CSV_SHAPES))
    def test_within_the_cpu_bound(self, label):
        outcome, cpu = check("csv", CSV_SHAPES[label].encode())
        assert outcome is True, outcome
        assert cpu < bound(CPU_BOUND), cpu

    @pytest.mark.parametrize(
        "label",
        [
            "accented-export",
            "accented-short-fields",
            "accented-long-fields",
            "hindi-long-fields",
            "hindi-40k-fields",
        ],
    )
    def test_non_ascii_text_costs_what_ascii_text_costs(self, label):
        """PARSERS2-1, relative (no scaling needed): the control is the same file with every
        non-ASCII character a "?", timed in the same process, and the cost is compared per byte
        (Hindi takes three bytes a letter). Lower-casing the buffer with str.translate at every
        chunk cost 5-18 times the control per byte; now 0.6-1.4 times, traced or not."""

        def cpu(data: bytes) -> float:  # the fastest of two runs
            return min(check("csv", data)[1] for _ in range(2))

        data = CSV_SHAPES[label].encode()
        twin = CSV_SHAPES[label].encode("ascii", "replace")
        control, spent = cpu(twin), cpu(data)
        assert spent < 3 * control * len(data) / len(twin) + bound(0.05), (spent, control)

    @pytest.mark.parametrize(
        ("unit", "tail"),
        [
            ("2026-10-12\tAsha;Lab,Call(s),x\n", ""),  # a call log split by every separator
            ('=IF(A1,"Call",0),', ""),  # short formula fields, each a candidate
            ('"=IF(A1,""Call"",0)",', ""),  # the same quoted
            ('"=A1"x,', "\nimage(\n"),  # quoted formulas with text after the quote
        ],
        ids=["call-log-every-separator", "formulas", "quoted-formulas", "continued-formulas"],
    )
    def test_the_budget_stops_the_costliest_shapes(self, unit, tail):
        """Shapes whose judging runs past CSV_CALL_WORK (2-8 s of CPU with no budget): refused as
        too large to check within the bound, never judged to the end."""
        content = (unit * ((LIMIT - len(tail.encode()) - 16) // len(unit.encode())) + tail).encode()
        outcome, cpu = check("csv", content)
        assert outcome == TOO_LARGE, outcome
        assert cpu < bound(CPU_BOUND), cpu

    def test_a_call_log_of_50_000_rows_passes(self):
        """PARSERS2-7: a field that can't be a formula (its first character, or the first after
        an opening quote, isn't = + - @) costs next to nothing."""
        row = "2026-10-12,Asha Lab,Call(s),spoke to manager; will revisit next week\n"
        outcome, cpu = check("csv", ("Date,Lead,Activity,Notes\n" + row * 50_000).encode())
        assert outcome is True, outcome
        assert cpu < bound(1.0), cpu


# --- workbooks and documents (PARSERS2-2) -------------------------------------------------------
def streamed(parts: list[tuple[str, str, str, int, str]], *, types: str) -> bytes:
    """A package whose parts are written as head + unit * count + tail, streamed (no big string)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as built:
        built.writestr("[Content_Types].xml", types)
        for name, head, unit, count, tail in parts:
            if not unit:
                built.writestr(name, head + tail)
                continue
            with built.open(name, "w", force_zip64=True) as part:
                part.write(head.encode())
                block = (unit * 65536).encode()
                left = count
                while left >= 65536:
                    part.write(block)
                    left -= 65536
                part.write((unit * left).encode() + tail.encode())
    return out.getvalue()


def workbook_types(sheets: int) -> str:
    overrides = "".join(
        f"<Override PartName='/xl/worksheets/sheet{n}.xml' ContentType='{WORKSHEET}'/>"
        for n in range(1, sheets + 1)
    )
    return (
        f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
        f"<Override PartName='/xl/workbook.xml' ContentType='{MAIN}'/>{overrides}</Types>"
    )


CELL_HEAD = f"<worksheet xmlns='{SM}'><sheetData><row r='1'><c r='A1'><v>"
COMMENTED_HEAD = f"<worksheet xmlns='{SM}'><!----><sheetData><row r='1'><c r='A1'><v>"
CELL_TAIL = "</v></c></row></sheetData></worksheet>"
WORKBOOK = ("xl/workbook.xml", f"<workbook xmlns='{SM}'/>", "", 0, "")
SHEET_BYTES = 256 * 1024 * 1024  # the old read budget, the most the reviews pushed through


def sheet_of(head: str, unit: str, count: int, tail: str = CELL_TAIL) -> bytes:
    return streamed(
        [WORKBOOK, ("xl/worksheets/sheet1.xml", head, unit, count, tail)], types=workbook_types(1)
    )


@functools.cache
def worst_workbook() -> bytes:
    """The formula scan's work in the pre-filter's slow path (a blank run before every bracket)
    plus 250 MiB of newlines parsed (a comment gets the part past the pre-filter): 8.4 s before."""
    spaced = 1_900_000
    return streamed(
        [
            WORKBOOK,
            ("xl/worksheets/sheet1.xml", CELL_HEAD, "x (", spaced, CELL_TAIL),
            (
                "xl/worksheets/sheet2.xml",
                COMMENTED_HEAD,
                "\n",
                SHEET_BYTES - 3 * spaced - 8192,
                CELL_TAIL,
            ),
        ],
        types=workbook_types(2),
    )


XLSX_SHAPES = {
    "newlines-parsed": lambda: sheet_of(COMMENTED_HEAD, "\n", SHEET_BYTES // 2),
    "text-parsed": lambda: sheet_of(COMMENTED_HEAD, "a", SHEET_BYTES // 2),
    "character-references": lambda: sheet_of(COMMENTED_HEAD, "&#65;", SHEET_BYTES // 10),
    "digits-read": lambda: sheet_of(CELL_HEAD, "1", SHEET_BYTES // 2),
    "tiny-elements": lambda: sheet_of(
        f"<worksheet xmlns='{SM}'><!----><sheetData>", "<a/>", 2_200_000, "</sheetData></worksheet>"
    ),
    "spaced-brackets": lambda: sheet_of(CELL_HEAD, "x (", 1_900_000),
    "spaced-names": lambda: sheet_of(CELL_HEAD, "importrang (", 1_900_000),
    "spaced-tab-names": lambda: sheet_of(CELL_HEAD, "imag\t(", 1_900_000),
    "long-formula": lambda: sheet_of(
        f"<worksheet xmlns='{SM}'><!----><sheetData><row r='1'><c r='A1'><f>",
        "A1+",
        20_000_000,
        "</f></c></row></sheetData></worksheet>",
    ),
    "worst-combined": worst_workbook,
}


class TestWorkbookCost:
    @pytest.mark.parametrize("label", sorted(XLSX_SHAPES))
    def test_within_the_cpu_bound(self, label):
        outcome, cpu = check("xlsx", XLSX_SHAPES[label]())
        assert outcome in (True, TOO_LARGE), outcome
        assert cpu < bound(CPU_BOUND), cpu

    def test_the_worst_document_with_the_worst_workbook_embedded(self):
        """64 MiB of newlines in a story (parsed: a comment) and the worst workbook embedded: one
        upload spending every budget at its most expensive unit (13.1 s before round 2)."""
        types = (
            f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
            f"<Default Extension='xlsx' ContentType='{XLSX_TYPE}'/>"
            f"<Override PartName='/word/document.xml' ContentType='{DOCMAIN}'/></Types>"
        )
        content = streamed(
            [
                (
                    "word/embeddings/Microsoft_Excel_Worksheet1.xlsx",
                    "",
                    "",
                    0,
                    "",
                ),
                (
                    "word/document.xml",
                    f"<w:document xmlns:w='{W}'><!----><w:body><w:p><w:r><w:t>",
                    "\n",
                    64 * 1024 * 1024 - 8192,
                    "</w:t></w:r></w:p></w:body></w:document>",
                ),
            ],
            types=types,
        )
        members = {}
        with zipfile.ZipFile(io.BytesIO(content)) as built:
            for name in built.namelist():
                members[name] = built.read(name)
        members["word/embeddings/Microsoft_Excel_Worksheet1.xlsx"] = worst_workbook()
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as built:
            for name, data in members.items():
                built.writestr(name, data)
        outcome, cpu = check("docx", out.getvalue())
        assert outcome in (True, TOO_LARGE), outcome
        assert cpu < bound(CPU_BOUND), cpu


# --- central directories (PARSERS2-3) -------------------------------------------------------------
def directory_heavy(target: int) -> bytes:
    """A zip with one stored empty member and a central directory repeating it until the package
    is about `target` bytes: zipfile parsed every record before the entry count was checked."""
    name = b"a"
    local = struct.pack("<4s5H3L2H", b"PK\x03\x04", 20, 0, 0, 0, 0, 0, 0, 0, len(name), 0) + name
    record = (
        struct.pack(
            "<4s6H3L5H2L", b"PK\x01\x02", 20, 20, 0, 0, 0, 0, 0, 0, 0, len(name), 0, 0, 0, 0, 0, 0
        )
        + name
    )
    count = (target - len(local) - 22) // len(record)
    directory = record * count
    end = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        min(count, 0xFFFE),
        min(count, 0xFFFE),
        len(directory),
        len(local),
        0,
    )
    return local + directory + end


def embedding(name: str, data: bytes) -> bytes:
    types = (
        f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
        f"<Default Extension='xlsx' ContentType='{XLSX_TYPE}'/>"
        f"<Override PartName='/word/document.xml' ContentType='{DOCMAIN}'/></Types>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as built:
        built.writestr("[Content_Types].xml", types)
        built.writestr("word/document.xml", f"<w:document xmlns:w='{W}'/>")
        built.writestr(name, data)
    return out.getvalue()


class TestCentralDirectories:
    def test_an_embedded_directory_bomb(self):
        """357,000 one-byte-name records in a 16 MiB embedded package (50 KB uploaded): 2.2 s
        and 149 MiB before; now refused from its end record, before zipfile parses a record."""
        content = embedding(
            "word/embeddings/w.xlsx", directory_heavy(storage.EMBEDDED_PACKAGE_BYTES - 1024)
        )
        outcome, cpu = check("docx", content)
        assert outcome == TOO_LARGE, outcome
        assert cpu < bound(0.5), cpu
        assert peak("docx", content) < MEMORY_BOUND

    def test_an_outer_directory_bomb(self):
        """223,000 records in a 10 MB upload: 83 MiB before."""
        content = directory_heavy(LIMIT - 1024)
        outcome, cpu = check("docx", content)
        assert outcome == TOO_LARGE, outcome
        assert cpu < bound(0.5), cpu
        assert peak("docx", content) < MEMORY_BOUND


# --- review round 3: bare names, small XML parts, many members (PARSERS3-1, PARSERS3-2) --------
PRES = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
PIVOT = "application/vnd.openxmlformats-officedocument.spreadsheetml.pivotCacheDefinition+xml"
RELS_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
FOOTER = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
PART = storage.OOXML_XML_PART_BYTES
WIDE_BLANK = chr(0x3000)  # IDEOGRAPHIC SPACE: a string holding it is two bytes a character
WORKBOOK_PART = ("xl/workbook.xml", f"<workbook xmlns='{SM}'/>")
DOCUMENT_PART = ("word/document.xml", f"<w:document xmlns:w='{W}'><w:body/></w:document>")


def bare_names(blank: str, blanks: int, tail: str = "") -> str:
    """A formula of FORMULA_CHARS characters: `blanks` blanks, then bare IMAGE names (each one a
    fetching name judged, none of them a call: IMAGE is a plausible range name, PARSERS3-3), then
    `tail`. The review used RTD, refused at its first name since round 3 (an operand)."""
    count = (storage.FORMULA_CHARS - blanks - len(tail)) // 6
    return blank * blanks + "IMAGE+" * count + tail


def members(parts: Sequence[tuple[str, str | bytes]], level: int = 9) -> bytes:
    """A zip of `parts`, in order, duplicate names included (on purpose)."""
    out = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about a duplicate name
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as built:
            for name, data in parts:
                built.writestr(name, data)
    return out.getvalue()


def formula_cells(formula: str, count: int) -> bytes:
    """A workbook whose sheet holds `count` cells of `formula` (a comment: the sheet is parsed)."""
    types = (
        f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
        f"<Override PartName='/xl/workbook.xml' ContentType='{MAIN}'/>"
        f"<Override PartName='/xl/worksheets/sheet1.xml' ContentType='{WORKSHEET}'/></Types>"
    )
    cells = f"<c><f>{formula}</f></c>" * count
    sheet = f"<worksheet xmlns='{SM}'><!----><sheetData><row r='1'>{cells}</row></sheetData>"
    return members(
        [
            ("[Content_Types].xml", types),
            WORKBOOK_PART,
            ("xl/worksheets/sheet1.xml", sheet + "</worksheet>"),
        ]
    )


def calculated_fields(formula: str, count: int) -> bytes:
    """A workbook whose pivot cache holds `count` calculated fields of `formula`."""
    types = (
        f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
        f"<Override PartName='/xl/workbook.xml' ContentType='{MAIN}'/>"
        f"<Override PartName='/xl/pivotCache/pivotCacheDefinition1.xml' ContentType='{PIVOT}'/>"
        "</Types>"
    )
    fields = f"<cacheField name='x' formula={quoteattr(formula)}/>" * count
    cache = f"<pivotCacheDefinition xmlns='{SM}'><!----><cacheFields>{fields}</cacheFields>"
    return members(
        [
            ("[Content_Types].xml", types),
            WORKBOOK_PART,
            ("xl/pivotCache/pivotCacheDefinition1.xml", cache + "</pivotCacheDefinition>"),
        ]
    )


def carrying(kind: str, inner: bytes) -> bytes:
    """A document (docx) or a presentation (pptx) with `inner` embedded as a workbook object."""
    main, main_type, root = {
        "docx": ("word/document.xml", DOCMAIN, f"<w:document xmlns:w='{W}'/>"),
        "pptx": ("ppt/presentation.xml", PRES, "<p:presentation xmlns:p='urn:p'/>"),
    }[kind]
    types = (
        f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
        f"<Default Extension='xlsx' ContentType='{XLSX_TYPE}'/>"
        f"<Override PartName='/{main}' ContentType='{main_type}'/></Types>"
    )
    embedded = f"{main.split('/')[0]}/embeddings/Microsoft_Excel_Worksheet1.xlsx"
    return members([("[Content_Types].xml", types), (main, root), (embedded, inner)])


def word_types(extra: str = "") -> str:
    return (
        f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
        f"<Default Extension='rels' ContentType='{RELS_TYPE}'/>"
        f"<Default Extension='xlsx' ContentType='{XLSX_TYPE}'/>"
        f"<Override PartName='/word/document.xml' ContentType='{DOCMAIN}'/>{extra}</Types>"
    )


def dense(head: str, tail: str, unit: str = "<a/>") -> str:
    """`head`, `unit` repeated to just under OOXML_XML_PART_BYTES, then `tail`."""
    return head + unit * ((PART - len(head) - len(tail) - 16) // len(unit)) + tail


def dense_types(unit: str = "<a/>") -> str:
    return dense(word_types()[: -len("</Types>")], "</Types>", unit)


INNER_TYPES = (
    f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
    f"<Override PartName='/word/document.xml' ContentType='{DOCMAIN}'/>"
)


def embedded_types(copies: int, outer: str | None = None) -> bytes:
    """A document embedding a package of `copies` element-dense [Content_Types].xml parts."""
    inner = members([("[Content_Types].xml", dense(INNER_TYPES, "</Types>"))] * copies)
    return members(
        [
            ("[Content_Types].xml", outer or word_types()),
            DOCUMENT_PART,
            ("word/embeddings/e.xlsx", inner),
        ]
    )


def tiny(count: int, name: str, data: str) -> list[tuple[str, str]]:
    return [(name.format(n), data) for n in range(count)]


TINY = storage.MAX_ZIP_ENTRIES - 3
COMMENTED = "<a><!----></a>"
# .rels parts declared a Word story (a footer) or a worksheet by a Default: every one of them is
# read as a relationships part and again as a story or a sheet.
RELS_AS_STORIES = word_types().replace(
    f"<Default Extension='rels' ContentType='{RELS_TYPE}'/>",
    f"<Default Extension='rels' ContentType='{FOOTER}'/>",
)
RELS_AS_SHEETS = (
    f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
    f"<Default Extension='rels' ContentType='{WORKSHEET}'/>"
    f"<Override PartName='/xl/workbook.xml' ContentType='{MAIN}'/></Types>"
)
INNER_RELS_TYPES = (
    f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
    f"<Default Extension='rels' ContentType='{WORKSHEET}'/>"
    f"<Default Extension='rels' ContentType='{FOOTER}'/>"
    f"<Override PartName='/word/document.xml' ContentType='{DOCMAIN}'/></Types>"
)


def tiny_embedded(count: int) -> bytes:
    inner_types = (
        f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
        f"<Override PartName='/word/document.xml' ContentType='{DOCMAIN}'/></Types>"
    )
    parts: list[tuple[str, str | bytes]] = [("[Content_Types].xml", word_types()), DOCUMENT_PART]
    for n in range(count):  # each one distinct, so deflate can't share them
        inner = members([("[Content_Types].xml", inner_types + f"<!--{n}-->")], level=0)
        parts.append((f"word/embeddings/e{n}.xlsx", inner))
    return members(parts)


def deep(head: str, tail: str, depth: int = 145_000) -> str:
    return head + "<a>" * depth + "</a>" * depth + tail


# Each found by review round 3 (CPU before, on the development laptop): (extension, builder).
ROUND3_SHAPES: dict[str, tuple[str, Callable[[], bytes]]] = {
    # PARSERS3-1: every bare name took a copy of the formula before it (18-31 s).
    "bare-names-ascii-blanks": ("xlsx", lambda: formula_cells(bare_names(" ", 32_768), 40)),
    "bare-names-wide-blanks": ("xlsx", lambda: formula_cells(bare_names(WIDE_BLANK, 32_768), 40)),
    "bare-names-wide-text": (
        "xlsx",
        lambda: formula_cells(bare_names(" ", 32_768, chr(0x1F600)), 40),
    ),
    "bare-names-no-blanks": ("xlsx", lambda: formula_cells(bare_names(" ", 0), 40)),
    "bare-names-in-a-document": (
        "docx",
        lambda: carrying("docx", formula_cells(bare_names(WIDE_BLANK, 32_768), 40)),
    ),
    "bare-names-in-a-presentation": (
        "pptx",
        lambda: carrying("pptx", formula_cells(bare_names(WIDE_BLANK, 32_768), 40)),
    ),
    "bare-names-in-calculated-fields": (
        "xlsx",
        lambda: calculated_fields(bare_names(WIDE_BLANK, 32_768), 40),
    ),
    # PARSERS3-2: small XML parts parsed whole into trees, again by every check (0.4-5.9 s,
    # 40-128 MiB traced).
    "dense-content-types": (
        "docx",
        lambda: members([("[Content_Types].xml", dense_types()), DOCUMENT_PART]),
    ),
    "duplicate-content-types": (
        "docx",
        lambda: members([("[Content_Types].xml", dense_types())] * 4 + [DOCUMENT_PART]),
    ),
    "dense-relationships": (
        "docx",
        lambda: members(
            [("[Content_Types].xml", word_types()), DOCUMENT_PART]
            + [
                (
                    f"word/_rels/r{n}.xml.rels",
                    dense(f"<Relationships xmlns='{PR}'>", "</Relationships>"),
                )
                for n in range(3)
            ]
        ),
    ),
    "attribute-dense-relationships": (
        "docx",
        lambda: members(
            [("[Content_Types].xml", word_types()), DOCUMENT_PART]
            + [
                (
                    f"word/_rels/r{n}.xml.rels",
                    f"<Relationships xmlns='{PR}'><a "
                    + " ".join(f"a{i}=''" for i in range(95_000))
                    + "/></Relationships>",
                )
                for n in range(3)
            ]
        ),
    ),
    "embedded-dense-content-types": ("docx", lambda: embedded_types(1)),
    "embedded-duplicate-content-types": ("docx", lambda: embedded_types(3)),
    "outer-and-embedded-content-types": ("docx", lambda: embedded_types(3, dense_types())),
    "deep-content-types": (
        "docx",
        lambda: members(
            [
                ("[Content_Types].xml", deep(word_types()[: -len("</Types>")], "</Types>")),
                DOCUMENT_PART,
            ]
        ),
    ),
    "deep-relationships": (
        "docx",
        lambda: members(
            [
                ("[Content_Types].xml", word_types()),
                DOCUMENT_PART,
                ("_rels/.rels", deep(f"<Relationships xmlns='{PR}'>", "</Relationships>")),
            ]
        ),
    ),
    # PARSERS3-2: members charged one unit each, opened and parsed several times (0.7-1.6 s).
    "tiny-story-parts": (
        "docx",
        lambda: members(
            [
                ("[Content_Types].xml", word_types()),
                DOCUMENT_PART,
                *tiny(TINY, "p/{}.xml", COMMENTED),
            ]
        ),
    ),
    "tiny-relationships-as-stories": (
        "docx",
        lambda: members(
            [
                ("[Content_Types].xml", RELS_AS_STORIES),
                DOCUMENT_PART,
                *tiny(TINY, "p/{}.rels", COMMENTED),
            ]
        ),
    ),
    "tiny-relationships-as-sheets": (
        "xlsx",
        lambda: members(
            [
                ("[Content_Types].xml", RELS_AS_SHEETS),
                WORKBOOK_PART,
                *tiny(TINY, "p/{}.rels", COMMENTED),
            ]
        ),
    ),
    "tiny-binary-members": (
        "docx",
        lambda: members(
            [("[Content_Types].xml", word_types()), DOCUMENT_PART, *tiny(TINY, "m/{}.dat", "x")]
        ),
    ),
    "embedded-tiny-relationships": (
        "docx",
        lambda: members(
            [
                ("[Content_Types].xml", word_types()),
                DOCUMENT_PART,
                (
                    "word/embeddings/e.xlsx",
                    members(
                        [
                            ("[Content_Types].xml", INNER_RELS_TYPES),
                            *tiny(storage.MAX_ZIP_ENTRIES - 10, "p/{}.rels", COMMENTED),
                        ]
                    ),
                ),
            ]
        ),
    ),
    "tiny-embedded-packages": ("docx", lambda: tiny_embedded(4990)),
}
# Shapes whose memory the review measured (40-128 MiB traced before round 3).
ROUND3_MEMORY = [
    "dense-content-types",
    "duplicate-content-types",
    "attribute-dense-relationships",
    "embedded-dense-content-types",
    "outer-and-embedded-content-types",
    "deep-content-types",
    "deep-relationships",
]


@functools.cache
def round3(label: str) -> bytes:
    return ROUND3_SHAPES[label][1]()


class TestReviewRound3Costs:
    @pytest.mark.parametrize("label", sorted(ROUND3_SHAPES))
    def test_within_the_cpu_bound(self, label):
        ext = ROUND3_SHAPES[label][0]
        outcome, cpu = check(ext, round3(label))
        assert outcome in (True, False, TOO_LARGE), outcome
        assert cpu < bound(CPU_BOUND), cpu

    @pytest.mark.parametrize("label", ROUND3_MEMORY)
    def test_within_the_memory_bound(self, label):
        assert peak(ROUND3_SHAPES[label][0], round3(label)) < MEMORY_BOUND

    @pytest.mark.parametrize(
        ("ext", "label"),
        [
            ("xlsx", "tiny-relationships-as-sheets"),
            ("docx", "tiny-relationships-as-stories"),
            ("docx", "embedded-tiny-relationships"),
            ("docx", "6000-tiny-embedded-packages"),
        ],
    )
    def test_member_floods_are_past_the_budget(self, ext, label):
        """PARSERS3-2: a member cost one unit whatever was done with it; now opening one
        (OPEN_WORK), a parser (PARSER_WORK) and an embedded package (EMBEDDED_WORK) are priced, so
        ten thousand members each opened and parsed several times (or 6,000 embedded packages)
        are past the budget (1.2-2.3 million units), deterministically, not let through after a
        second or two of CPU."""
        content = tiny_embedded(6_000) if label.startswith("6000") else round3(label)
        assert check(ext, content)[0] == TOO_LARGE

    def test_an_embedded_package_is_priced(self, monkeypatch):
        """An embedded package takes 100-160 us of CPU here (opened, its directory read, its
        content types scanned, every check walking it): at least 150 units each, so the most an
        upload can hold (about 5,000, the entries budget) stays within the pool's microsecond a
        unit. One unit each, it was 0.8 s for 520,000 units (PARSERS3-2)."""
        spent: list[storage._PackageBudget] = []

        class Recorded(storage._PackageBudget):
            __slots__ = ()

            def __init__(self) -> None:
                super().__init__()
                spent.append(self)

        monkeypatch.setattr(storage, "_PackageBudget", Recorded)

        def units(content: bytes) -> int:
            spent.clear()
            assert check("docx", content)[0] is True
            return sum(storage.UPLOAD_WORK - budget.pool.left for budget in spent)

        assert (units(tiny_embedded(200)) - units(tiny_embedded(0))) / 200 >= 150

    @pytest.mark.parametrize("label", ["dense-content-types", "dense-relationships"])
    def test_a_small_part_has_an_element_cap(self, label):
        """SMALL_PART_ELEMENTS: a 1 MiB part of real declarations or relationships holds fewer
        than 10,000 elements; one of 260,000 empty elements is too large to check."""
        assert check("docx", round3(label))[0] == TOO_LARGE

    @pytest.mark.parametrize(
        "names",
        [
            ["[Content_Types].xml", "[Content_Types].xml"],
            ["word/document.xml", "WORD/Document.XML"],
            ["word/document.xml", "word/%64ocument.xml"],
            ["word/_rels/document.xml.rels", "/word/_rels/document.xml.rels"],
        ],
        ids=["same", "letter-case", "percent-encoded", "leading-slash"],
    )
    def test_duplicate_part_names_are_refused(self, names):
        """OPC part names are equivalent ignoring ASCII case, percent-decoded: Office refuses a
        package naming one part twice, and each copy was parsed by every check (PARSERS3-2)."""
        parts = [("[Content_Types].xml", word_types()), DOCUMENT_PART]
        parts += [(name, "<Relationships xmlns='urn:r'/>") for name in names]
        outcome, _ = check("docx", members(parts))
        assert outcome is False

    def test_content_types_are_read_once_a_package(self, monkeypatch):
        """PARSERS3-2: [Content_Types].xml was parsed by _active_content and again by each content
        check (four times for an embedded package): now once a package, then reused."""
        inner = members(
            [
                ("[Content_Types].xml", INNER_TYPES + "</Types>"),
                ("word/document.xml", f"<w:document xmlns:w='{W}'/>"),
            ]
        )
        content = members(
            [
                ("[Content_Types].xml", word_types()),
                DOCUMENT_PART,
                ("word/embeddings/e.xlsx", inner),
            ]
        )
        scanned: list[str] = []
        scan = storage._scan_part
        monkeypatch.setattr(
            storage,
            "_scan_part",
            lambda raw, budget, kind: scanned.append(kind) or scan(raw, budget, kind),
        )
        assert check("docx", content)[0] is True
        assert scanned.count("types") == 2  # the document's and the embedded package's

    def test_a_formula_of_bare_names_is_judged_in_linear_time(self):
        """PARSERS3-1: 32K wide blanks then thousands of bare names cost 0.5-0.8 s to judge (each
        name took a copy of the text before it); now a few milliseconds."""
        formula = bare_names(WIDE_BLANK, 32_768)
        started = time.process_time()
        for _ in range(10):
            assert storage._formula_calls(formula, blanks=True, whole=True) == (False, 5_461)
        assert (time.process_time() - started) / 10 < bound(0.02)


# --- the per-upload CPU deadline (review round 3, structural) ----------------------------------
def burn(seconds: float) -> None:
    """Spend `seconds` of CPU (a busy loop): an unpriced slow path, injected."""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


def slowed(function: Callable[..., object], seconds: float) -> Callable[..., object]:
    def run(*args: object, **kwargs: object) -> object:
        burn(seconds)
        return function(*args, **kwargs)

    return run


class SlowReads(io.BytesIO):
    """An upload whose every read costs CPU: whatever loop reads it is slow."""

    def read(self, size: int | None = -1) -> bytes:
        burn(0.004)
        return super().read(size)


DEADLINE = 0.3  # seconds, for these tests (UPLOAD_CPU_SECONDS)
STOPPED = 1.0  # what an upload stopped by it may have cost, at most


def minimal(ext: str) -> bytes:
    if ext == "csv":
        return b"Name,City\nAsha,Pune\n"
    if ext == "xlsx":
        return formula_cells("SUM(A1)", 1)
    if ext == "pptx":
        return carrying("pptx", formula_cells("SUM(A1)", 1))
    return members([("[Content_Types].xml", word_types()), DOCUMENT_PART])


class TestUploadDeadline:
    """Defence in depth (review round 3: unit pricing kept missing unpriced paths): every check
    of one upload runs against a CPU deadline (UPLOAD_CPU_SECONDS of this thread's CPU from the
    start of the check), looked at in every loop that can run long; past it the upload is "too
    large to check". The unit budgets stay the primary, deterministic bound."""

    @pytest.mark.parametrize("ext", ["docx", "xlsx", "pptx", "csv"])
    def test_every_check_runs_against_the_clock(self, monkeypatch, ext):
        ticks = iter([0.0])  # the deadline is set from the first reading; every later one is past
        monkeypatch.setattr(storage, "_cpu_clock", lambda: next(ticks, 1e9))
        assert check(ext, minimal(ext))[0] == TOO_LARGE

    def test_ordinary_uploads_are_far_from_it(self):
        assert storage.UPLOAD_CPU_SECONDS >= 3 * CPU_BOUND / 2
        for ext in ("docx", "xlsx", "pptx", "csv"):
            assert check(ext, minimal(ext))[0] is True

    @pytest.mark.parametrize(
        ("target", "attribute", "seconds", "ext", "content"),
        [
            # A formula judged slowly, a thousand of them in one chunk of the sheet.
            (
                "storage",
                "_formula_calls",
                0.003,
                "xlsx",
                lambda: formula_cells("SUM(A1)", 1_000),
            ),
            # A Word field judged slowly, 800 of them in one chunk.
            (
                "storage",
                "_field_rule",
                0.004,
                "docx",
                lambda: members(
                    [
                        ("[Content_Types].xml", word_types()),
                        (
                            "word/document.xml",
                            f"<w:document xmlns:w='{W}'><w:body><w:p>"
                            + "<w:fldSimple w:instr='PAGE'><w:r><w:t>1</w:t></w:r></w:fldSimple>"
                            * 800
                            + "</w:p></w:body></w:document>",
                        ),
                    ]
                ),
            ),
            # An element parsed slowly, 300,000 of them over many chunks, nothing judged.
            (
                "formulas",
                "on_start",
                0.00001,
                "xlsx",
                lambda: sheet_of(
                    f"<worksheet xmlns='{SM}'><!---->", "<a/>", 300_000, "</worksheet>"
                ),
            ),
            # A member looked at slowly, a thousand of them.
            (
                "storage",
                "_held",
                0.003,
                "docx",
                lambda: members(
                    [
                        ("[Content_Types].xml", word_types()),
                        DOCUMENT_PART,
                        *tiny(1000, "m/{}.dat", "x"),
                    ]
                ),
            ),
            # An embedded package opened slowly, 300 of them.
            ("storage", "_directory_too_large", 0.01, "docx", lambda: tiny_embedded(300)),
            # A CSV formula judged slowly, a thousand of them in one chunk.
            (
                "storage",
                "_formula_calls",
                0.003,
                "csv",
                lambda: ("a,b\n" + 'x,"=IF(A1,""Call"",0)"\n' * 1_000).encode(),
            ),
            # A name decoded slowly (review round 4): the duplicate-name pass, a thousand.
            (
                "storage",
                "unquote",
                0.003,
                "docx",
                lambda: members(
                    [
                        ("[Content_Types].xml", word_types()),
                        DOCUMENT_PART,
                        *tiny(1_000, "m/{}", "x"),
                    ]
                ),
            ),
            # The same in an embedded package's name set: a thousand members inside it.
            (
                "storage",
                "unquote",
                0.003,
                "docx",
                lambda: document_with(
                    members(
                        [
                            ("[Content_Types].xml", INNER_TYPES + "</Types>"),
                            *tiny(1_000, "m/{}", "x"),
                        ]
                    ),
                    1,
                ),
            ),
        ],
        ids=[
            "formula", "field", "element", "member", "embedded", "csv-formula",
            "duplicate-pass", "embedded-names",
        ],
    )  # fmt: skip
    def test_a_slow_path_is_stopped(self, monkeypatch, target, attribute, seconds, ext, content):
        owner = storage if target == "storage" else storage._Formulas
        monkeypatch.setattr(owner, attribute, slowed(getattr(owner, attribute), seconds))
        monkeypatch.setattr(storage, "UPLOAD_CPU_SECONDS", DEADLINE)
        outcome, cpu = check(ext, content())
        assert outcome == TOO_LARGE, outcome
        assert cpu < STOPPED, cpu

    def test_slow_reads_are_stopped(self, monkeypatch):
        """Every loop that reads a CSV looks at the clock: a read that costs CPU (here 4 ms each)
        stops the check at the deadline, whichever reading is running."""
        content = ("sep=|\nname|note\n" + "Asha|a!b =IMAGE(\n" * 300_000).encode()[:LIMIT]
        monkeypatch.setattr(storage, "UPLOAD_CPU_SECONDS", DEADLINE)
        started = time.process_time()
        try:
            outcome: object = storage.CATALOG["csv"].recognise(content[:8192], SlowReads(content))
        except storage.RejectedFile as refused:
            outcome = str(refused)
        assert outcome == TOO_LARGE, outcome
        assert time.process_time() - started < STOPPED


# --- review round 4: directory records (PARSERS4-1) --------------------------------------------
LONG_EXTRA = b"\xfe\xca\x00\x00" * 16_383  # 65,532 bytes: 16,383 empty records of no known type
PERCENT_NAME = "%" * 65_000


def with_records(
    main: tuple[str, str],
    types: str,
    count: int,
    *,
    extra: bytes = b"",
    name: Callable[[int], str] | None = None,
) -> bytes:
    """A package of `main`, its content types and `count` members whose directory records carry
    `extra` as their extra field, or a name built by `name`."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as built:
        built.writestr("[Content_Types].xml", types)
        built.writestr(*main)
        for n in range(count):
            info = zipfile.ZipInfo(name(n) if name else f"docProps/pad{n:02d}.txt")
            info.compress_type = zipfile.ZIP_DEFLATED
            info.extra = extra
            built.writestr(info, "x")
    return out.getvalue()


def workbook_records(
    count: int, *, extra: bytes = b"", name: Callable[[int], str] | None = None
) -> bytes:
    return with_records(WORKBOOK_PART, workbook_types(0), count, extra=extra, name=name)


def document_with(inner: bytes, copies: int) -> bytes:
    return members(
        [("[Content_Types].xml", word_types()), DOCUMENT_PART]
        + [(f"word/embeddings/Microsoft_Excel_Worksheet{n}.xlsx", inner) for n in range(copies)]
    )


ROUND4_SHAPES: dict[str, tuple[str, Callable[[], bytes]]] = {
    # 3.6-3.8 s and accepted before: 12 embedded workbooks, 17 records with 64 KiB extra fields.
    "embedded-extra-fields": (
        "docx",
        lambda: document_with(workbook_records(17, extra=LONG_EXTRA), 12),
    ),
    # 4.4-4.5 s and accepted before: 4 embedded workbooks, 19 records named with 65,000 "%".
    "embedded-percent-names": (
        "docx",
        lambda: document_with(
            workbook_records(19, name=lambda n: f"docProps/{n:02d}{PERCENT_NAME}.txt"), 4
        ),
    ),
    "outer-extra-fields": (
        "docx",
        lambda: with_records(
            ("word/document.xml", f"<w:document xmlns:w='{W}'/>"),
            word_types(),
            17,
            extra=LONG_EXTRA,
        ),
    ),
    "outer-percent-names": (
        "docx",
        lambda: with_records(
            ("word/document.xml", f"<w:document xmlns:w='{W}'/>"),
            word_types(),
            19,
            name=lambda n: f"docProps/{n:02d}{PERCENT_NAME}.txt",
        ),
    ),
}


class TestReviewRound4Directories:
    @pytest.mark.parametrize("label", sorted(ROUND4_SHAPES))
    def test_too_large_under_the_units_alone(self, monkeypatch, label):
        """The deadline was all that stopped these (its longest stretch 0.6 s); with a clock
        that never runs out, the directory's records are measured before zipfile parses them."""
        monkeypatch.setattr(storage, "_cpu_clock", lambda: 0.0)
        ext, build = ROUND4_SHAPES[label]
        outcome, cpu = check(ext, build())
        assert outcome == TOO_LARGE, outcome
        assert cpu < bound(CPU_BOUND), cpu

    def test_ordinary_records_pass(self):
        """Office's names stay under 200 bytes and its extra fields under 40 (a ZIP64 or NTFS
        timestamp record): records of a 200-byte name and a 36-byte extra field pass."""
        ntfs = struct.pack("<2H", 0x000A, 32) + b"\x00" * 32
        content = with_records(
            ("word/document.xml", f"<w:document xmlns:w='{W}'/>"),
            word_types(),
            20,
            extra=ntfs,
            name=lambda n: f"word/media/{n:02d}-" + "a" * 180 + ".png",
        )
        assert check("docx", content)[0] is True
        assert check("docx", document_with(workbook_records(20, extra=ntfs), 2))[0] is True

    @pytest.mark.parametrize("field", ["name", "extra", "comment"])
    def test_a_field_just_over_the_limit_is_too_large(self, field):
        limit = storage.DIRECTORY_FIELD_BYTES
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as built:
            built.writestr("[Content_Types].xml", word_types())
            built.writestr("word/document.xml", f"<w:document xmlns:w='{W}'/>")
            info = zipfile.ZipInfo("a" * (limit + 1) if field == "name" else "a.txt")
            if field == "extra":
                info.extra = b"\xfe\xca" + struct.pack("<H", limit - 3) + b"\x00" * (limit - 3)
            if field == "comment":
                info.comment = b"c" * (limit + 1)
            built.writestr(info, "x")
        assert check("docx", out.getvalue())[0] == TOO_LARGE


class TestReviewRound4Records:
    def test_more_records_than_entries_are_refused_before_zipfile_reads_them(self, monkeypatch):
        """An end record declaring one entry over a directory of 12,000 records (within the bytes
        10,000 entries may take): zipfile parsed every record before the entries could be counted
        (0.08 s, 10 MiB, review round 3); now the records are counted first."""
        name = b"a"
        fields = (b"PK\x01\x02", 20, 20, 0, 0, 0, 0, 0, 0, 0, len(name), 0, 0, 0, 0, 0, 0)
        record = struct.pack("<4s6H3L5H2L", *fields) + name
        local = struct.pack("<4s5H3L2H", b"PK\x03\x04", 20, 0, 0, 0, 0, 0, 0, 0, 1, 0) + name
        directory = record * 12_000
        end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 1, 1, len(directory), len(local), 0)

        def parsed(*_: object, **__: object) -> None:
            raise AssertionError("zipfile parsed the directory")

        monkeypatch.setattr(storage.zipfile, "ZipFile", parsed)
        assert check("docx", local + directory + end)[0] == TOO_LARGE

    def test_a_directory_is_priced_by_its_bytes(self, monkeypatch):
        """zipfile's parse of a record costs with its length: the directory's bytes are paid for
        (PARSE_BYTES_PER_WORK a unit), so 1,000 records of 900-byte names cost some 14,000 units
        more than 1,000 of short ones."""
        spent: list[storage._PackageBudget] = []

        class Recorded(storage._PackageBudget):
            __slots__ = ()

            def __init__(self) -> None:
                super().__init__()
                spent.append(self)

        monkeypatch.setattr(storage, "_PackageBudget", Recorded)

        def units(width: int) -> int:
            content = with_records(
                ("word/document.xml", f"<w:document xmlns:w='{W}'/>"),
                word_types(),
                1_000,
                name=lambda n: f"m/{n:04d}" + "a" * width,
            )
            spent.clear()
            assert check("docx", content)[0] is True
            return sum(storage.UPLOAD_WORK - budget.pool.left for budget in spent)

        assert units(900) - units(10) >= 1_000 * 890 // storage.PARSE_BYTES_PER_WORK
