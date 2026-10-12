"""Uploaded files that run something when another user opens them (R132, the deep security
audit's M-3 residual; docs/activities.md#attachments): Word fields that start a program or
fetch content from elsewhere (DDE, DDEAUTO, INCLUDETEXT, INCLUDEPICTURE, LINK, IMPORT, INCLUDE,
DATABASE) in any story of a document, a workbook's DDE link, a CSV cell Excel would run as a
DDE formula. Each refused variant trips one rule only; ordinary documents keep passing."""

from __future__ import annotations

import codecs
import io
import time
import tracemalloc
import zipfile
from urllib.parse import quote

import pytest

from arkray.activities import storage
from arkray.activities.models import Attachment
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory

pytestmark = pytest.mark.django_db

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
STRICT = "http://purl.oclc.org/ooxml/wordprocessingml/main"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NAMESPACES = (
    f'xmlns:w="{W}" xmlns:mc="{MC}"'
    ' xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
    ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
    ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    ' xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
    ' xmlns:v="urn:schemas-microsoft-com:vml" mc:Ignorable="w14"'
)
DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
ML = "application/vnd.openxmlformats-officedocument.wordprocessingml."
DDE = r'DDEAUTO c:\\windows\\system32\\cmd.exe "/k calc.exe"'


@pytest.fixture
def note(user_a):
    lead = LeadFactory(owner=user_a)
    return NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)


def upload(client, note, name, content):
    return client.post(
        f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
        content,
        content_type="application/octet-stream",
        HTTP_X_FILENAME=quote(name, safe=""),
    )


def refusal(response) -> str:
    assert response.status_code == 400, response.content
    assert not Attachment.objects.exists()
    return str(response.json()["error"]["details"]["file"][0])


# --- Word's XML, as Word writes it ----------------------------------------------------------------
def run(*content: str, properties: str = "") -> str:
    return f"<w:r>{properties}{''.join(content)}</w:r>"


def char(kind: str) -> str:
    return run(f'<w:fldChar w:fldCharType="{kind}"/>')


def code(text: str) -> str:
    return run(f'<w:instrText xml:space="preserve">{text}</w:instrText>')


def text(value: str) -> str:
    return run(f'<w:t xml:space="preserve">{value}</w:t>')


def field(*codes: str, result: str = "1") -> str:
    """A complex field: its code split over one run per string, a result, an end."""
    return (
        char("begin")
        + "".join(code(part) for part in codes)
        + char("separate")
        + text(result)
        + char("end")
    )


def simple(instruction: str, result: str = "1") -> str:
    return f'<w:fldSimple w:instr="{instruction}">{text(result)}</w:fldSimple>'


def paragraph(*content: str) -> str:
    return f"<w:p>{''.join(content)}</w:p>"


def document(*content: str, root: str = "document") -> str:
    body = "".join(content)
    if root == "document":
        body = f"<w:body>{body}</w:body>"
    return f"{DECLARATION}<w:{root} {NAMESPACES}>{body}</w:{root}>"


def text_box(*content: str) -> str:
    """A text box as Word writes it: DrawingML in mc:Choice, VML in mc:Fallback, each with the
    same paragraphs (a story of its own)."""
    paragraphs = "".join(content)
    return run(
        '<mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wp:anchor><a:graphic>'
        '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
        f"<wps:wsp><wps:txbx><w:txbxContent>{paragraphs}</w:txbxContent></wps:txbx></wps:wsp>"
        "</a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice><mc:Fallback><w:pict>"
        f"<v:shape><v:textbox><w:txbxContent>{paragraphs}</w:txbxContent></v:textbox></v:shape>"
        "</w:pict></mc:Fallback></mc:AlternateContent>"
    )


def rules(xml: str | bytes) -> set[str] | None:
    """Every rule the part trips (not only the first), or None if it can't be read."""
    tripped: set[str] = set()
    budget = storage._Budget(storage.WORD_XML_BYTES, storage.WORD_XML_WORK)
    raw = xml.encode() if isinstance(xml, str) else xml
    readable = storage._stream_story(io.BytesIO(raw), budget, storage._Story(tripped.add))
    return tripped if readable else None


# --- packages -------------------------------------------------------------------------------------
def content_types(*overrides: tuple[str, str], defaults: tuple[tuple[str, str], ...] = ()) -> bytes:
    declared = (
        ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
        ("xml", "application/xml"),
        ("png", "image/png"),
        *defaults,
    )
    return (
        codecs.BOM_UTF8
        + (
            f"{DECLARATION}<Types"
            ' xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            + "".join(f'<Default Extension="{e}" ContentType="{t}"/>' for e, t in declared)
            + "".join(f'<Override PartName="{p}" ContentType="{t}"/>' for p, t in overrides)
            + "</Types>"
        ).encode()
    )


def relationships(*targets: tuple[str, str]) -> str:
    return (
        f"{DECLARATION}<Relationships"
        ' xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{n}" Type="{R}{kind}" Target="{target}"/>'
            for n, (kind, target) in enumerate(targets, 1)
        )
        + "</Relationships>"
    )


STORY_TYPES = (
    ("/word/document.xml", ML + "document.main+xml"),
    ("/word/styles.xml", ML + "styles+xml"),
    ("/word/settings.xml", ML + "settings+xml"),
    ("/word/header1.xml", ML + "header+xml"),
    ("/word/footer1.xml", ML + "footer+xml"),
    ("/word/footnotes.xml", ML + "footnotes+xml"),
    ("/word/endnotes.xml", ML + "endnotes+xml"),
    ("/word/comments.xml", ML + "comments+xml"),
    ("/word/glossary/document.xml", ML + "document.glossary+xml"),
    ("/docProps/core.xml", "application/vnd.openxmlformats-package.core-properties+xml"),
)
NOTES = '<w:{0} w:id="{1}">' + paragraph(text("A note.")) + "</w:{0}>"


def word_parts(**stories: str) -> dict[str, str | bytes]:
    """A document shaped like what Word writes: a body with a table of contents, hyperlinks,
    merge fields and a text box; page numbers in the footer; footnotes, endnotes, comments,
    building blocks, styles, settings, properties, custom XML. `stories` replaces parts."""
    toc_entry = paragraph(
        char("begin"),
        code(r' HYPERLINK \l "_Toc1" '),
        char("separate"),
        text("Prices"),
        field(r" PAGEREF _Toc1 \h ", result="2"),
        char("end"),
    )
    body = document(
        paragraph(char("begin"), code(r' TOC \o "1-3" \h \z \u '), char("separate")),
        toc_entry,
        paragraph(char("end")),
        paragraph(text("Dear "), field(r" MERGEFIELD Lab_Name \* MERGEFORMAT ", result="«Lab»")),
        paragraph(field(r' HYPERLINK "https://www.arkray.example/strips" ', result="strips")),
        paragraph(field(r' DATE \@ "d MMMM yyyy" ', result="12 October 2026")),
        paragraph(text_box(paragraph(field(" PAGE ")))),
        '<w:sectPr><w:headerReference w:type="default" r:id="rId8"/></w:sectPr>',
    )
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": content_types(*STORY_TYPES),
        "_rels/.rels": relationships(("officeDocument", "word/document.xml")),
        "word/_rels/document.xml.rels": relationships(
            ("styles", "styles.xml"),
            ("settings", "settings.xml"),
            ("header", "header1.xml"),
            ("footer", "footer1.xml"),
            ("footnotes", "footnotes.xml"),
            ("endnotes", "endnotes.xml"),
            ("comments", "comments.xml"),
            ("glossaryDocument", "glossary/document.xml"),
        ),
        "word/document.xml": body,
        "word/styles.xml": document(root="styles"),
        "word/settings.xml": document('<w:zoom w:percent="100"/>', root="settings"),
        "word/header1.xml": document(paragraph(text("Arkray")), root="hdr"),
        "word/footer1.xml": document(
            paragraph(text("Page "), simple(" PAGE "), text(" of "), field(" NUMPAGES ")),
            root="ftr",
        ),
        "word/footnotes.xml": document(NOTES.format("footnote", 1), root="footnotes"),
        "word/endnotes.xml": document(NOTES.format("endnote", 1), root="endnotes"),
        "word/comments.xml": document(NOTES.format("comment", 0), root="comments"),
        "word/glossary/document.xml": document(
            "<w:docParts><w:docPart><w:docPartBody>"
            + paragraph(field(" AUTHOR "))
            + "</w:docPartBody></w:docPart></w:docParts>",
            root="glossaryDocument",
        ),
        "docProps/core.xml": f"{DECLARATION}<cp:coreProperties xmlns:cp='urn:cp'/>",
        "customXml/item1.xml": f"{DECLARATION}<b:Sources xmlns:b='urn:b'/>",
    }
    parts.update(stories)
    return parts


def package(parts: dict[str, str | bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as built:
        for name, content in parts.items():
            built.writestr(name, content)
    return out.getvalue()


# --- the rules, one part at a time ----------------------------------------------------------------
UNC = r'INCLUDETEXT "\\\\fileserver\\share\\terms.docx"'
QUOTED_DDE = DDE.replace('"', "&quot;")
BEGIN = '<w:fldChar w:fldCharType="begin"/>'
END = '<w:fldChar w:fldCharType="end"/>'
INSTR_X = "<w:instrText>x</w:instrText>"
GLOW = "<w:r><w:rPr><w14:glow w14:rad='63500'/></w:rPr>"
TAB = "<w:r><w:tab/></w:r>"
BR = "<w:r><w:br/></w:r>"
TRIMMED = "<w:r><w:instrText>{}</w:instrText></w:r>"  # no xml:space="preserve"


def branch(content: str) -> str:
    """Run content in an mc:Choice (Word reads it, or the Fallback, by its version)."""
    return run(
        f"<mc:AlternateContent><mc:Choice Requires='w14'>{content}</mc:Choice>"
        "<mc:Fallback/></mc:AlternateContent>"
    )


REFUSED = [
    # DDE and DDEAUTO, always: plain, any case, blanks before, split over runs, fldSimple, Strict.
    ("dde", "dde", paragraph(field(r'DDE excel "c:\\prices.xlsx" "R1C1"'))),
    ("ddeauto", "dde", paragraph(field(DDE))),
    ("lowercase", "dde", paragraph(field(DDE.lower()))),
    ("blanks-before", "dde", paragraph(field(" \t  " + DDE))),
    ("split-runs", "dde", paragraph(field("D", "DE", "AU", "TO", r" c:\\windows\\cmd.exe"))),
    ("fldsimple", "dde", paragraph(simple(DDE.replace('"', "&quot;")))),
    ("code-in-run-text", "dde", paragraph(char("begin"), text(DDE), char("end"))),
    ("no-separator", "dde", paragraph(char("begin"), code(DDE), char("end"))),
    ("never-ended", "dde", paragraph(char("begin"), code(DDE))),
    ("stray-code", "dde", paragraph(code(DDE))),
    (
        "nested-in-result",
        "dde",
        paragraph(field(" TOC ", result="x").replace(text("x"), field(DDE))),
    ),
    (
        "nested-in-code",
        "dde",
        paragraph(char("begin"), code(" IF 1 = 1 "), field(DDE), char("end")),
    ),
    ("text-box", "dde", paragraph(text_box(paragraph(field(DDE))))),
    (
        "around-a-text-box",  # a story of its own: its end and begin don't touch the body's code
        "dde",
        paragraph(
            char("begin"),
            code("DD"),
            text_box(paragraph(char("end"), char("begin"))),
            code(DDE[2:]),
            char("end"),
        ),
    ),
    ("no-type", "dde", paragraph(run("<w:fldChar/>"), code(DDE), char("end"))),
    (
        "deleted-field",  # a deleted field is there until the change is accepted
        "dde",
        paragraph(
            '<w:del w:id="1" w:author="A">',
            char("begin"),
            run(f"<w:delInstrText>{DDE}</w:delInstrText>"),
            char("end"),
            "</w:del>",
        ),
    ),
    # Fetching fields with an argument naming another place.
    (
        "includepicture-http",
        "remote",
        paragraph(field('INCLUDEPICTURE "http://evil.example/p.png"')),
    ),
    ("unc", "remote", paragraph(field(UNC))),
    ("forward-slashes", "remote", paragraph(field('INCLUDETEXT "//fileserver/share/t.docx"'))),
    ("file-url", "remote", paragraph(field('INCLUDETEXT "file://fileserver/share/t.docx"'))),
    ("old-include", "remote", paragraph(field('INCLUDE "https://evil.example/t.docx"'))),
    ("import", "remote", paragraph(field(r"IMPORT \\\\evil.example\\p.wmf"))),
    (
        "link",
        "remote",
        paragraph(field(r'LINK Excel.Sheet.12 "\\\\evil\\s\\b.xlsx" "Sheet1!R1C1:R2C2" \a \f 4')),
    ),
    ("unquoted", "remote", paragraph(field(r"INCLUDEPICTURE https://evil.example/p.png \d"))),
    ("padded-quotes", "remote", paragraph(field('INCLUDEPICTURE "  http://evil.example/p"'))),
    ("other-scheme", "remote", paragraph(field('INCLUDEPICTURE "mhtml:http://evil.example/x"'))),
    ("split-target", "remote", paragraph(field("INCLUDEPICTURE ", '"htt', 'p://evil.example/p"'))),
    ("database", "database", paragraph(field(r'DATABASE \d "C:\\data.mdb" \s "SELECT 1"'))),
    # A name that isn't a literal word, or a fetching field's argument that isn't one.
    (
        "quote-obfuscation",  # the 2017 DDE obfuscation: the name is a nested QUOTE's result
        "computed",
        paragraph(
            char("begin"),
            field("QUOTE 68 68 69 65 85 84 79", result="DDEAUTO"),
            code(r' c:\\windows\\system32\\cmd.exe "/k calc"'),
            char("end"),
        ),
    ),
    (
        "name-half-nested",
        "computed",
        paragraph(char("begin"), code("DD"), field(" QUOTE 69 "), code("AUTO x"), char("end")),
    ),
    ("quoted-name", "computed", paragraph(field('"DDEAUTO" c:\\\\x'))),
    (
        "symbol-in-name",
        "computed",
        paragraph(
            char("begin"),
            run('<w:sym w:font="Arial" w:char="0044"/>'),
            code("DEAUTO x"),
            char("end"),
        ),
    ),
    (
        "computed-target",
        "computed",
        paragraph(
            char("begin"),
            code('INCLUDEPICTURE "'),
            field(" MERGEFIELD Photo ", result="p.png"),
            code('" \\d'),
            char("end"),
        ),
    ),
    (
        "cut-code",
        "computed",
        paragraph(field('INCLUDETEXT "chapter2.docx" ' + "x" * storage.FIELD_CODE_CHARS)),
    ),
    # A tab, a line break or a paragraph mark in a code: read as a blank and as nothing
    # (independent review: each of these passed when it only joined).
    ("mark-in-name", "dde", paragraph(char("begin"), code("DD")) + paragraph(code(DDE[2:]))),
    ("tab-in-name", "dde", paragraph(char("begin"), code("DD"), TAB, code(DDE[2:]))),
    ("tab-after-name", "dde", paragraph(char("begin"), code("DDEAUTO"), TAB, code(" cmd x"))),
    ("break-after-name", "dde", paragraph(char("begin"), code("DDEAUTO"), BR, code("cmd x"))),
    (
        "mark-after-name",
        "dde",
        paragraph(char("begin"), code("DDEAUTO")) + paragraph(code("cmd x"), char("end")),
    ),
    (
        "untrimmed-runs",  # no xml:space="preserve": Word trims " E" to "E", so DD + E
        "dde",
        paragraph(char("begin"), TRIMMED.format("DD"), TRIMMED.format(" E cmd x"), char("end")),
    ),
    (
        "link-tab-package",
        "remote",
        paragraph(
            char("begin"), code("LINK"), TAB, code(r'Package "\\\\host\\s\\x" "" \a'), char("end")
        ),
    ),
    (
        "tab-in-include",
        "remote",
        paragraph(char("begin"), code("INCLUDE"), TAB, code('PICTURE "http://e.x/p"'), char("end")),
    ),
    (
        "tab-in-address",
        "remote",
        paragraph(char("begin"), code('INCLUDEPICTURE "ht'), TAB, code('tp://e.x/p"'), char("end")),
    ),
    (
        "tab-before-colon",  # "http ://" names nowhere; joined, http:// does
        "remote",
        paragraph(char("begin"), code('INCLUDEPICTURE "http'), TAB, code('://e.x/p"'), char("end")),
    ),
    (
        "tab-before-address",  # a blank to Word: INCLUDEPICTURE, then its address
        "remote",
        paragraph(char("begin"), code("INCLUDEPICTURE"), TAB, code("http://e.x/p"), char("end")),
    ),
    (
        "mark-before-address",
        "remote",
        paragraph(char("begin"), code("INCLUDEPICTURE"))
        + paragraph(code("http://e.x/p"), char("end")),
    ),
    # What Word may or may not read must not cut across a field.
    ("branch-in-code", "structure", paragraph(char("begin"), code(" PAGE "), branch(INSTR_X))),
    ("field-leaves-branch", "structure", paragraph(branch(BEGIN + INSTR_X), char("end"))),
    ("end-in-branch", "structure", paragraph(field(" PAGE ").replace(char("end"), branch(END)))),
    (
        "ignorable-wrapper",  # Word skips an element of an ignorable namespace it doesn't know
        "structure",
        paragraph(f"<x:skip xmlns:x='urn:x' mc:Ignorable='x'><x:in>{char('end')}</x:in></x:skip>"),
    ),
    (
        "two-types",  # Transitional and Strict disagree
        "structure",
        paragraph(run(f'<w:fldChar w:fldCharType="end" xmlns:s="{STRICT}" s:fldCharType="x"/>')),
    ),
    ("two-codes", "structure", paragraph(f'<w:fldSimple w:instr=" PAGE " instr="{QUOTED_DDE}"/>')),
    (
        "too-deep",
        "structure",
        paragraph(*[char("begin") + code(" IF ")] * (storage.FIELD_DEPTH + 1))
        + paragraph(*[char("end")] * (storage.FIELD_DEPTH + 1)),
    ),
    # A tracked change: a code reads one way as recorded, another with every change
    # accepted or rejected.
    (
        "deleted-part",
        "remote",
        paragraph(
            char("begin"),
            code("INCLUDE"),
            '<w:del w:id="1" w:author="A"><w:r><w:delInstrText>X</w:delInstrText></w:r></w:del>',
            code('PICTURE "http://evil.example/p.png"'),
            char("end"),
        ),
    ),
    (
        "inserted-part",
        "remote",
        paragraph(
            char("begin"),
            code("INCLUDE"),
            f'<w:ins w:id="1" w:author="A">{code("X")}</w:ins>',
            code('PICTURE "http://evil.example/p.png"'),
            char("end"),
        ),
    ),
    (
        "deleted-prefix",  # as recorded "XDDEAUTO", an unknown name; accepted, DDEAUTO
        "dde",
        paragraph(
            char("begin"),
            '<w:del w:id="1" w:author="A"><w:r><w:delInstrText>X</w:delInstrText></w:r></w:del>',
            code(DDE),
            char("end"),
        ),
    ),
    (
        "inserted-prefix",  # rejected, DDEAUTO
        "dde",
        paragraph(
            char("begin"),
            f'<w:ins w:id="1" w:author="A">{code("X")}</w:ins>',
            code(DDE),
            char("end"),
        ),
    ),
]
ACCEPTED = [
    ("page", paragraph(field(" PAGE "), simple(" NUMPAGES \\* Arabic "))),
    (
        "toc",
        paragraph(
            char("begin"),
            code(r' TOC \o "1-3" \h \z \u '),
            char("separate"),
            field(r' HYPERLINK \l "_Toc1" ', result="Prices"),
            field(r" PAGEREF _Toc1 \h "),
            char("end"),
        ),
    ),
    ("ref", paragraph(field(r" REF _Ref123 \h "))),
    ("hyperlink", paragraph(field(r' HYPERLINK "https://www.arkray.example/" \o "Arkray" '))),
    ("mergefield", paragraph(field(r" MERGEFIELD Lab_Name \* MERGEFORMAT ", result="«Lab_Name»"))),
    ("date", paragraph(field(r' DATE \@ "dd/MM/yyyy" '))),
    (
        "form",
        paragraph(
            run(
                '<w:fldChar w:fldCharType="begin"><w:ffData><w:name w:val="Text1"/>'
                "</w:ffData></w:fldChar>"
            ),
            code(" FORMTEXT "),
            char("separate"),
            text("x"),
            char("end"),
        ),
    ),
    ("formula", paragraph(field(r' = SUM(ABOVE) \# "#,##0.00" '))),
    (
        "if-merge",
        paragraph(
            char("begin"),
            code(" IF "),
            field(" MERGEFIELD City "),
            code(' = "Pune" "Local" "Outstation" '),
            char("separate"),
            text("Local"),
            char("end"),
        ),
    ),
    ("seq", paragraph(field(r" SEQ Figure \* ARABIC "))),
    (
        "local-picture",
        paragraph(
            field(
                r'INCLUDEPICTURE "C:\\Users\\me\\AppData\\Local\\Temp\\msohtmlclip1\\01\\clip.png"'
                r" \* MERGEFORMATINET"
            )
        ),
    ),
    (
        "mac-picture",
        paragraph(
            field(
                r'INCLUDEPICTURE "/var/folders/x/T/com.microsoft.Word/WebArchiveCopyPaste/p.png"'
                r" \* MERGEFORMATINET"
            )
        ),
    ),
    (
        "local-link",
        paragraph(
            field(
                r'LINK Excel.Sheet.12 "C:\\Users\\me\\Documents\\Prices.xlsx"'
                r' "Sheet1!R1C1:R5C3" \a \f 4 \h'
            )
        ),
    ),
    ("relative-include", paragraph(field('INCLUDETEXT "chapter2.docx" Terms'))),
    (
        "citation",
        paragraph(
            field('ADDIN ZOTERO_ITEM CSL_CITATION {"uris":["http://zotero.org/users/1/items/ABC"]}')
        ),
    ),
    ("empty", paragraph(char("begin"), char("end"), simple(""))),
    ("text-box", paragraph(text_box(paragraph(field(" PAGE "))))),
    (
        "strict",
        paragraph(field(" PAGE "))
        .replace("<w:", "<s:")
        .replace("</w:", "</s:")
        .replace("w:fldCharType", "s:fldCharType")
        .replace("<s:p>", f'<s:p xmlns:s="{STRICT}">'),
    ),
    (
        "tracked-toc",  # a table of contents updated with changes tracked: the old one deleted
        paragraph(
            '<w:del w:id="1" w:author="A">',
            run('<w:fldChar w:fldCharType="begin"/>'),
            run(r'<w:delInstrText> TOC \o "1-3" </w:delInstrText>'),
            run('<w:fldChar w:fldCharType="separate"/>'),
            run("<w:delText>old</w:delText>"),
            run('<w:fldChar w:fldCharType="end"/>'),
            "</w:del>",
            f'<w:ins w:id="2" w:author="A">{field(r" TOC \o 1-3 ")}</w:ins>',
        ),
    ),
    (
        "text-effects",  # an element of an ignorable namespace in a code, holding no field content
        paragraph(char("begin"), code(" PAGE ").replace("<w:r>", GLOW), char("end")),
    ),
    (
        "tab-in-ordinary",
        paragraph(char("begin"), code("PAGE"), TAB, code(r"\* MERGEFORMAT"), char("end")),
    ),
    ("trimmed-code", paragraph(char("begin"), TRIMMED.format("  PAGE  "), char("end"))),
    (
        "code-over-paragraphs",
        paragraph(char("begin"), code(" IF 1 = 1 ")) + paragraph(code('"a" "b" '), char("end")),
    ),
    (
        "ignorable-scope-ended",  # declared ignorable inside an element: not after it
        "<w:p xmlns:p='urn:p' mc:Ignorable='p'/>"
        + paragraph(f"<p:wrap xmlns:p='urn:p'>{field(' PAGE ')}</p:wrap>"),
    ),
]


class TestFieldRules:
    @pytest.mark.parametrize(("label", "rule", "body"), REFUSED, ids=[case[0] for case in REFUSED])
    def test_refused_variants_trip_one_rule(self, label, rule, body):
        assert rules(document(body)) == {rule}

    @pytest.mark.parametrize(("label", "body"), ACCEPTED, ids=[case[0] for case in ACCEPTED])
    def test_ordinary_fields_pass(self, label, body):
        assert rules(document(body)) == set()

    def test_utf16_story_parts_are_read(self):
        for encoding, bom in (
            ("utf-16-le", codecs.BOM_UTF16_LE),
            ("utf-16-be", codecs.BOM_UTF16_BE),
        ):
            xml = document(paragraph(field(DDE))).replace("UTF-8", "UTF-16")
            assert rules(bom + xml.encode(encoding)) == {"dde"}
            ordinary = document(paragraph(field(" PAGE "))).replace("UTF-8", "UTF-16")
            assert rules(bom + ordinary.encode(encoding)) == set()

    @pytest.mark.parametrize(
        "raw",
        [
            document(paragraph(field(" PAGE "))).replace("UTF-8", "UTF-16").encode(),  # disagrees
            document(paragraph(field(" PAGE "))).replace("UTF-8", "ISO-8859-1").encode(),
            document(paragraph(field(" PAGE "))).encode("utf-16-le"),  # UTF-16 without its mark
            document(paragraph(field(" PAGE "))).encode()[:-20],  # cut short
            b"\xff" + document(paragraph(field(" PAGE "))).encode(),  # not UTF-8
            f'<!DOCTYPE d [<!ENTITY e "DDEAUTO x">]><w:document {NAMESPACES}/>'.encode(),
            document(paragraph(char("begin"), code("&e;"), char("end"))).encode(),  # undefined
        ],
        ids=["declared-utf16", "latin1", "utf16-no-bom", "truncated", "bad-utf8", "dtd", "entity"],
    )
    def test_a_part_read_differently_from_word_is_unreadable(self, raw):
        assert rules(raw) is None

    def test_blanks_word_trims_are_trimmed(self, monkeypatch):
        """Without xml:space="preserve" Word trims the blanks around a run's text (independent
        review P2-3): the code read is the one Word reads."""
        read = []
        judge = storage._field_rule

        def recording(code, *, complete=True):
            read.append(code)
            return judge(code, complete=complete)

        monkeypatch.setattr(storage, "_field_rule", recording)
        rules(document(paragraph(char("begin"), TRIMMED.format(" DD "), TRIMMED.format(" E x "))))
        rules(document(paragraph(char("begin"), code(" DD "), code(" E x "))))
        assert read == ["DDE x\x01", "DD  E x \x01"]  # the paragraph mark: a break

    def test_judging_a_code(self):
        rule = storage._field_rule
        assert rule("") is None
        assert rule("   ") is None
        assert rule(" page ") is None
        assert rule("=2*3") is None
        assert rule(r"PAGE\* MERGEFORMAT") is None
        assert rule('DDEAUTO"c:\\x"') == "dde"
        assert rule("DDEX") == "dde"  # read bare, any name starting DDE (fail closed)
        assert rule("D\x01DE x") == "dde"  # a break between letters
        assert rule('LINK\x01Package "\\\\h\\s"') == "remote"
        assert rule("PAGE\x01\\* MERGEFORMAT") is None
        assert rule('INCLUDEPICTURE "C:\\\\x.png"') is None  # a drive letter isn't a scheme
        assert rule('INCLUDEPICTURE "c:/x.png"') is None
        assert rule('INCLUDEPICTURE "ms-word:ofe|u|https://e.x/t.docx"') == "remote"
        assert rule('INCLUDEPICTURE "\\\\?\\C:\\x.png"') == "remote"  # two backslashes: refused
        assert rule("INCLUDETEXT x", complete=False) == "computed"
        assert rule("PAGE x", complete=False) is None
        assert rule("\x00 PAGE") == "computed"
        assert rule("INCLUDETEXT\x00") == "computed"  # the name runs into a nested field


class TestWordUploads:
    """Through the upload: every story part, under any name, in any OPC encoding."""

    def test_an_ordinary_document_with_fields_everywhere_passes(self, user_a_client, note):
        response = upload(user_a_client, note, "proposal.docx", package(word_parts()))
        assert response.status_code == 201, response.content

    @pytest.mark.parametrize(
        ("part", "root", "wrap"),
        [
            ("word/document.xml", "document", paragraph),
            ("word/header1.xml", "hdr", paragraph),
            ("word/footer1.xml", "ftr", paragraph),
            (
                "word/footnotes.xml",
                "footnotes",
                lambda body: f'<w:footnote w:id="2">{paragraph(body)}</w:footnote>',
            ),
            (
                "word/endnotes.xml",
                "endnotes",
                lambda body: f'<w:endnote w:id="2">{paragraph(body)}</w:endnote>',
            ),
            (
                "word/comments.xml",
                "comments",
                lambda body: f'<w:comment w:id="1">{paragraph(body)}</w:comment>',
            ),
            (
                "word/glossary/document.xml",
                "glossaryDocument",
                lambda body: (
                    f"<w:docParts><w:docPart><w:docPartBody>{paragraph(body)}</w:docPartBody></w:docPart></w:docParts>"
                ),
            ),
        ],
    )
    def test_a_dangerous_field_in_any_story_is_refused(self, user_a_client, note, part, root, wrap):
        parts = word_parts(**{part: document(wrap(field(DDE)), root=root)})
        message = refusal(upload(user_a_client, note, "proposal.docx", package(parts)))
        assert message == storage.FIELD_REFUSED

    def test_a_renamed_story_is_found_by_its_declared_type(self, user_a_client, note):
        """A header under a name that isn't *.xml: found by its Override, or its Default."""
        header = document(
            paragraph(field('INCLUDEPICTURE "http://evil.example/p.png"')), root="hdr"
        )
        by_override = word_parts(**{"word/h.dat": header})
        by_override["[Content_Types].xml"] = content_types(
            *STORY_TYPES, ("/word/h.dat", ML + "header+xml")
        )
        by_default = word_parts(**{"word/page.hdr": header})
        by_default["[Content_Types].xml"] = content_types(
            *STORY_TYPES, defaults=(("hdr", ML + "header+xml"),)
        )
        for parts in (by_override, by_default):
            assert (
                refusal(upload(user_a_client, note, "proposal.docx", package(parts)))
                == storage.FIELD_REFUSED
            )

    def test_a_utf16_story_part(self, user_a_client, note):
        def footer(body):
            xml = document(paragraph(body), root="ftr").replace("UTF-8", "UTF-16")
            return codecs.BOM_UTF16_LE + xml.encode("utf-16-le")

        dangerous = word_parts(**{"word/footer1.xml": footer(field(DDE))})
        assert (
            refusal(upload(user_a_client, note, "b.docx", package(dangerous)))
            == storage.FIELD_REFUSED
        )
        ordinary = word_parts(**{"word/footer1.xml": footer(field(" PAGE "))})
        assert upload(user_a_client, note, "a.docx", package(ordinary)).status_code == 201

    def test_an_unreadable_story_is_refused_as_not_what_it_says(self, user_a_client, note):
        parts = word_parts(
            **{
                "word/header1.xml": document(paragraph(field(" PAGE ")), root="hdr").replace(
                    "UTF-8", "UTF-16"
                )
            }
        )
        assert "doesn't match its type" in refusal(
            upload(user_a_client, note, "a.docx", package(parts))
        )

    def test_parts_that_never_mention_a_field_are_not_parsed(self, user_a_client, note):
        """No fldChar, fldSimple or instrText in a part's text, and no "<!": Word can't find a
        field in it (an element's name is never escaped), so it is decoded but not parsed."""
        parts = word_parts(
            **{"customXml/item2.xml": f"{DECLARATION}<b:Sources xmlns:b='urn:b'><b:Source>"}
        )
        assert upload(user_a_client, note, "a.docx", package(parts)).status_code == 201
        assert (
            storage._mentions_fields(
                io.BytesIO(b"<w:fld" + b"x" * 70_000 + b"Char/>"), storage._Budget(10**9, 0)
            )
            is False
        )
        split = b"x" * (storage.CHUNK_BYTES - 3) + b"fldChar"  # "fld" ends the first chunk
        assert storage._mentions_fields(io.BytesIO(split), storage._Budget(10**9, 0)) is True

    def test_past_the_byte_budget_the_document_is_refused_unread(
        self, user_a_client, note, monkeypatch
    ):
        monkeypatch.setattr(storage, "WORD_XML_BYTES", 8 * 1024)
        parts = word_parts(
            **{"word/header1.xml": document(paragraph(text("x" * 10_000)), root="hdr")}
        )
        assert (
            refusal(upload(user_a_client, note, "a.docx", package(parts)))
            == storage.TOO_LARGE_TO_CHECK
        )
        budget = storage._Budget(100, 0)
        with pytest.raises(storage._OverBudget):
            list(storage._decoded(io.BytesIO(b"x" * 101), budget))

    def test_past_the_work_budget_the_document_is_refused(self, user_a_client, note, monkeypatch):
        """What parsing costs is bounded too: elements, and field content per reading."""
        monkeypatch.setattr(storage, "WORD_XML_WORK", 60)
        many = word_parts(
            **{"word/header1.xml": document(paragraph(*[field(" PAGE ")] * 5), root="hdr")}
        )
        assert (
            refusal(upload(user_a_client, note, "a.docx", package(many)))
            == storage.TOO_LARGE_TO_CHECK
        )

    def test_other_office_types_are_not_read_for_fields(self, user_a_client, note, settings):
        settings.ATTACHMENT_ALLOWED_EXTENSIONS = ["docx", "xlsx", "pptx"]
        slide = document(paragraph(field(DDE)))  # Word markup in a deck means nothing to PowerPoint
        parts = {
            "[Content_Types].xml": content_types(),
            "ppt/presentation.xml": "<p:presentation xmlns:p='urn:p'/>",
            "ppt/slides/slide1.xml": slide,
        }
        assert upload(user_a_client, note, "deck.pptx", package(parts)).status_code == 201


class TestSpreadsheetDde:
    """Excel saves a DDE formula (=cmd|' /c calc'!A0) as an external link part holding a
    ddeLink; the cell refers to it. Today's rules refuse that by its part name, its declared
    type and its relationship: each alone is enough."""

    LINK = (
        f"{DECLARATION}<externalLink"
        ' xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<ddeLink ddeService="cmd" ddeTopic=" /c calc"><ddeItems>'
        '<ddeItem name="A0" advise="1"><values rows="1" cols="1"><value t="n"><val>0</val>'
        "</value></values></ddeItem></ddeItems></ddeLink></externalLink>"
    )
    EXTERNAL = "application/vnd.openxmlformats-officedocument.spreadsheetml.externalLink+xml"

    def workbook(self, link_part, *, declared=True, related=True):
        return package(
            {
                "[Content_Types].xml": content_types(
                    (
                        "/xl/workbook.xml",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                    ),
                    *([(f"/{link_part}", self.EXTERNAL)] if declared else []),
                ),
                "_rels/.rels": relationships(("officeDocument", "xl/workbook.xml")),
                "xl/workbook.xml": (
                    "<workbook xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'"
                    f" xmlns:r='{R[:-1]}'><sheets><sheet name='Sheet1' sheetId='1'"
                    " r:id='rId1'/></sheets><externalReferences>"
                    "<externalReference r:id='rId2'/></externalReferences></workbook>"
                ),
                "xl/_rels/workbook.xml.rels": relationships(
                    ("worksheet", "worksheets/sheet1.xml"),
                    *([("externalLink", link_part.removeprefix("xl/"))] if related else []),
                ),
                "xl/worksheets/sheet1.xml": (
                    "<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>"
                    "<sheetData><row r='1'><c r='A1'><f>[1]!'!A0'</f><v>0</v></c></row></sheetData>"
                    "</worksheet>"
                ),
                link_part: self.LINK,
            }
        )

    def test_as_excel_saves_it(self, user_a_client, note):
        content = self.workbook("xl/externalLinks/externalLink1.xml")
        assert "doesn't match its type" in refusal(
            upload(user_a_client, note, "prices.xlsx", content)
        )

    @pytest.mark.parametrize(
        ("link_part", "declared", "related"),
        [
            ("xl/externalLinks/externalLink1.xml", False, False),  # the part's name alone
            ("xl/links/l1.xml", True, False),  # its declared type alone
            ("xl/links/l1.xml", False, True),  # its relationship alone
        ],
    )
    def test_each_sign_alone(self, user_a_client, note, link_part, declared, related):
        content = self.workbook(link_part, declared=declared, related=related)
        assert "doesn't match its type" in refusal(
            upload(user_a_client, note, "prices.xlsx", content)
        )


class TestXmlBounds:
    """Independent review P1-1 and P1-2: whatever the budget, the XML itself is bounded: a start
    tag (or attribute value, or comment) expat would hold whole, attributes on one element,
    nesting, mc:Ignorable scopes; and attributes are paid for."""

    @staticmethod
    def built(body: str) -> bytes:
        """A document whose body is `body` after a field (so its part is parsed)."""
        return package(
            word_parts(**{"word/document.xml": document(paragraph(field(" PAGE ")), body)})
        )

    @staticmethod
    def recognised(content: bytes) -> object:
        """The docx check: True, False, or the refusal's message."""
        try:
            return storage.CATALOG["docx"].recognise(content[:8192], io.BytesIO(content))
        except storage.RejectedFile as refused:
            return str(refused)

    def check(self, body: str) -> object:
        return self.recognised(self.built(body))

    def test_one_huge_start_tag_is_refused_with_bounded_memory(self):
        tag = "<w:p " + " ".join(f'a{n}=""' for n in range(600_000)) + "/>"  # about 5 MiB
        content = self.built(tag)
        tracemalloc.start()
        try:
            outcome = self.recognised(content)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert outcome == storage.TOO_LARGE_TO_CHECK
        assert peak < 24 * 1024 * 1024, peak  # unbounded, this tag held ~150 MB

    def test_a_huge_comment_or_attribute_value_is_refused(self):
        assert self.check("<!--" + "x" * (storage.XML_TOKEN_BYTES + 1) + "-->") == (
            storage.TOO_LARGE_TO_CHECK
        )
        assert self.check('<w:p w:rsidR="' + "x" * (storage.XML_TOKEN_BYTES + 1) + '"/>') == (
            storage.TOO_LARGE_TO_CHECK
        )

    def test_a_long_text_is_not_a_token(self):
        assert self.check(paragraph(text("x" * (2 * storage.XML_TOKEN_BYTES)))) is True

    def test_attributes(self, monkeypatch):
        many = "<w:p " + " ".join(f'a{n}=""' for n in range(storage.XML_ATTRIBUTES + 1)) + "/>"
        assert self.check(many) == storage.TOO_LARGE_TO_CHECK
        enough = "<w:p " + " ".join(f'a{n}=""' for n in range(storage.XML_ATTRIBUTES)) + "/>"
        assert self.check(enough) is True
        monkeypatch.setattr(storage, "WORD_XML_WORK", 5_000)  # each attribute is paid for
        assert self.check(enough * 10) == storage.TOO_LARGE_TO_CHECK

    def test_nesting(self):
        deep = storage.XML_DEPTH - 10
        assert self.check("<a>" * deep + "</a>" * deep) is True
        deeper = storage.XML_DEPTH + 1
        assert self.check("<a>" * deeper + "</a>" * deeper) == storage.TOO_LARGE_TO_CHECK

    @pytest.mark.parametrize("shape", ["complex", "simple", "tracked"])
    def test_long_field_codes_are_paid_for(self, shape):
        """Independent review, round 2: judging a code is work too (1 unit per
        FIELD_CODE_CHARS_PER_WORK characters, per reading). 1,000 INCLUDETEXT codes of 32,000
        arguments took 71 s and passed; 400 fldSimple ones 23 s; 50 in tracked changes 1.8 s (two
        readings see an insertion: 80 of them reach the budget)."""
        instruction = "INCLUDETEXT" + " a" * 32_000
        one = paragraph(char("begin"), code(instruction), char("end"))
        if shape == "complex":
            body = one * 400
        elif shape == "simple":
            body = paragraph(f'<w:fldSimple w:instr="{instruction}"/>' * 400)
        else:
            body = f'<w:ins w:id="1" w:author="A">{one * 80}</w:ins>'
        content = self.built(body)
        started = time.process_time()
        assert self.recognised(content) == storage.TOO_LARGE_TO_CHECK
        assert time.process_time() - started < 15  # was 23-71 s; under 1 s at full clock

    def test_one_search_finds_an_address_anywhere(self):
        address = "INCLUDETEXT" + " a" * 30_000 + ' "http://e.x/t.docx"'
        assert storage._field_rule(address) == "remote"
        assert storage._field_rule(address.replace("http:", "C:")) is None

    def test_distinct_names(self):
        """expat keeps a table entry for every distinct element and attribute name: a part
        with more than XML_NAMES of them is refused (300,000 held ~80 MB)."""
        assert self.check("".join(f"<t{n}/>" for n in range(2_000))) is True
        many = "".join(f"<t{n}/>" for n in range(storage.XML_NAMES + 1))
        assert self.check(many) == storage.TOO_LARGE_TO_CHECK

    def test_ignorable_scopes_cost_what_they_add(self):
        """30,000 nested mc:Ignorable cost ~45 s before (each recomputed every scope's
        union); a declaration that adds nothing keeps no scope, the set in effect is restored
        at a scope's end, and at most IGNORABLE_DEPTH add a namespace inside one another."""
        n = storage.IGNORABLE_DEPTH + 8
        same = "<a xmlns:x='urn:x' mc:Ignorable='x'>" * n + "</a>" * n
        assert self.check(same) is True
        empty = "<a mc:Ignorable=''>" * n + "</a>" * n
        assert self.check(empty) is True
        as_value = f"<a v='{storage._IGNORABLE}'>" * n + "</a>" * n  # a value, not the name
        assert self.check(as_value) is True
        adding = "".join(f"<a xmlns:p{d}='urn:{d}' mc:Ignorable='p{d}'>" for d in range(n))
        assert self.check(adding + "</a>" * n) == storage.TOO_LARGE_TO_CHECK
        shallow = "".join(
            f"<a xmlns:p{d}='urn:{d}' mc:Ignorable='p{d}'><x:in xmlns:x='urn:x'/></a>"
            for d in range(n)
        )
        assert self.check(shallow) is True  # one at a time: each scope ends

    def test_a_scope_kept_is_paid_for(self, monkeypatch):
        """Independent review, third pass: sibling scopes each emptied the name memo twice,
        about 2.3x a real document's cost per unit of work; a scope kept now also pays
        IGNORABLE_SCOPE_WORK, so the budget bounds them like everything else."""
        siblings = "".join(
            f"<a xmlns:p{d % 8}='urn:{d % 8}' mc:Ignorable='p{d % 8}'/>" for d in range(2_000)
        )
        assert self.check(siblings) is True
        # About 5 units each without the charge (10,000 in all), over 20 with it.
        monkeypatch.setattr(storage, "WORD_XML_WORK", 25_000)
        assert self.check(siblings) == storage.TOO_LARGE_TO_CHECK


class TestExcelMacroSheets:
    """Excel 4.0 (XLM) macro sheets run macros without a VBA project (Auto_Open): refused by
    their declared type or their relationship, international ones too, each sign alone."""

    MACRO = "application/vnd.ms-excel.macrosheet+xml"
    INTL = "application/vnd.ms-excel.intlmacrosheet+xml"
    MS = "http://schemas.microsoft.com/office/2006/relationships/"
    SHEET = (
        "<xm:macrosheet xmlns:xm='http://schemas.microsoft.com/office/excel/2006/main'>"
        "<sheetData><row r='1'><c r='A1'><f>EXEC(\"calc.exe\")</f></c></row></sheetData>"
        "</xm:macrosheet>"
    )

    def workbook(self, *, declared="", related=""):
        main = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
        overrides = [("/xl/workbook.xml", main)]
        if declared:
            overrides.append(("/xl/macrosheets/sheet1.xml", declared))
        rels = relationships(("worksheet", "worksheets/sheet1.xml"))
        if related:
            rels = rels.replace(
                "</Relationships>",
                f'<Relationship Id="rId9" Type="{self.MS}{related}"'
                ' Target="macrosheets/sheet1.xml"/></Relationships>',
            )
        return package(
            {
                "[Content_Types].xml": content_types(*overrides),
                "_rels/.rels": relationships(("officeDocument", "xl/workbook.xml")),
                "xl/workbook.xml": "<workbook xmlns='urn:x'/>",
                "xl/_rels/workbook.xml.rels": rels,
                "xl/worksheets/sheet1.xml": "<worksheet xmlns='urn:x'/>",
                "xl/macrosheets/sheet1.xml": self.SHEET,
            }
        )

    @pytest.mark.parametrize(
        ("declared", "related"),
        [
            (MACRO, "xlMacrosheet"),  # as Excel saves one
            (MACRO, ""),  # the declared type alone
            (INTL, ""),
            ("", "xlMacrosheet"),  # the relationship alone
            ("", "xlIntlMacrosheet"),
        ],
        ids=["as-saved", "type", "intl-type", "relationship", "intl-relationship"],
    )
    def test_refused(self, user_a_client, note, declared, related):
        content = self.workbook(declared=declared, related=related)
        assert "doesn't match its type" in refusal(upload(user_a_client, note, "a.xlsx", content))

    def test_without_either_sign_the_same_package_passes(self, user_a_client, note):
        """Neither declared nor related, the part is no macro sheet to Excel (it loads parts
        through relationships, by their type): the refusals above are the signs' doing."""
        assert upload(user_a_client, note, "a.xlsx", self.workbook()).status_code == 201


class TestCsvDde:
    DDE_CELLS = [
        "=cmd|' /C calc'!A0",
        "+cmd|' /C calc'!A0",
        "-2+3+cmd|' /C calc'!A0",
        "@SUM(1+1)*cmd|' /C calc'!A0",
        "=MSEXCEL|'\\..\\..\\Windows\\System32\\cmd.exe /c calc.exe'!''",
        ' \t\r=cmd|"/c calc"!A0',  # blanks, a tab, a line break before it
        "=\"it's\"&cmd|'/c calc'!A0",
    ]
    ORDINARY_CELLS = [
        "-5",
        "+91 98765 43210",
        "@name",
        "=SUM(A1:A3)",
        '=A1&"|"&B1&"!"',  # the "|" is in a string literal
        "='Q1|Q2'!A1",  # in a quoted sheet name
        "a|b!c",  # not a formula
        "=cmd|' /C calc'",  # no "!" after the "|": not DDE's shape
        '=HYPERLINK("https://lab.example/?a|b!c")',
        "Ravi | call back!",
    ]

    @pytest.mark.parametrize("cell", DDE_CELLS)
    def test_a_dde_cell_is_refused(self, user_a_client, note, cell):
        row = '"' + cell.replace('"', '""') + '"'
        content = f"name,formula\nCity Lab,{row}\n".encode()
        assert refusal(upload(user_a_client, note, "list.csv", content)) == storage.CSV_DDE_REFUSED

    def test_ordinary_cells_pass(self, user_a_client, note):
        rows = "\n".join(
            f'City Lab {n},"{cell.replace(chr(34), chr(34) * 2)}"'
            for n, cell in enumerate(self.ORDINARY_CELLS)
        )
        content = f"name,value\n{rows}\n".encode()
        assert upload(user_a_client, note, "list.csv", content).status_code == 201

    @pytest.mark.parametrize(
        "content",
        [
            "name;formula\nCity Lab;=cmd|' /C calc'!A0\n",  # Excel's separator in many regions
            "name\tformula\nCity Lab\t=cmd|' /C calc'!A0\n",
            "sep=#\nname#formula\nCity Lab#=cmd|' /C calc'!A0\n",  # the separator Excel is told
            chr(0xFEFF) + "name,formula\r\nCity Lab,=cmd|' /C calc'!A0\r\n",  # a BOM, CRLF
            'name,formula\n"City\nLab","=cmd|\' /C calc\'!A0"\n',  # a quoted line break before it
        ],
        ids=["semicolon", "tab", "sep-line", "bom-crlf", "multiline"],
    )
    def test_whatever_separates_the_cells(self, user_a_client, note, content):
        assert (
            refusal(upload(user_a_client, note, "list.csv", content.encode()))
            == storage.CSV_DDE_REFUSED
        )

    def test_a_line_excel_reads_as_one_cell(self, user_a_client, note):
        """Commas only: where Excel's separator is a semicolon, each line is one cell."""
        content = b"=A1,b|c!d\n"
        assert refusal(upload(user_a_client, note, "list.csv", content)) == storage.CSV_DDE_REFUSED

    def test_unquoted_pipe_rule(self):
        assert storage._dde_cell("=cmd|' /C calc'!A0")
        assert not storage._dde_cell('=A1&"|"&"!"')
        assert not storage._dde_cell("='a|b'!A1")
        assert storage._dde_cell("='a''|b'|x!A1")  # a doubled quote stays inside the name
        assert not storage._dde_cell('="unclosed|x!')  # an unclosed literal hides the rest
        assert not storage._dde_cell("x=cmd|a!b")
        assert not storage._dde_cell("=cmd|' /C calc'")  # no "!" after the "|"

    def test_long_fields_are_read_across_chunks(self, user_a_client, note, settings):
        """Fields longer than a chunk (64 KiB) and than CSV_FIELD_CHARS, quoted with "" inside
        or not, held in memory or spooled to disk: what follows them is still read."""
        quoted = '"' + 'say ""x"", |\n' * 20_000 + '"'  # 240 KB: "", commas, lines inside
        plain = "y|" * 100_000  # 200 KB, not a formula
        dde = "Other,=cmd|' /C calc'!A0\n"
        content = f"name,notes\nCity Lab,{quoted}\nTown Lab,{plain}\n{dde}".encode()
        assert refusal(upload(user_a_client, note, "list.csv", content)) == storage.CSV_DDE_REFUSED
        settings.ATTACHMENT_SPOOL_MEMORY_BYTES = 1024
        assert refusal(upload(user_a_client, note, "list.csv", content)) == storage.CSV_DDE_REFUSED
        ordinary = content.replace(b"=cmd", b"cmd") + b"Last,!\n"
        assert upload(user_a_client, note, "list.csv", ordinary).status_code == 201

    @pytest.mark.parametrize(
        "cell",
        [
            # Decided before its "|" is read: a formula longer than Excel holds, blanks that far.
            "=" + "a" * (storage.CSV_FIELD_CHARS + 3 * storage.CHUNK_BYTES) + "|x!y",
            " " * (storage.CSV_FIELD_CHARS + 3 * storage.CHUNK_BYTES) + "=cmd|x!y",
        ],
        ids=["long-formula", "long-blanks"],
    )
    def test_a_field_too_long_to_judge_fails_closed(self, user_a_client, note, cell):
        content = f"name,formula\nCity Lab,{cell}\n".encode()
        assert refusal(upload(user_a_client, note, "list.csv", content)) == storage.CSV_DDE_REFUSED

    def test_a_long_quoted_cells_lines_are_not_cells(self, user_a_client, note):
        """A quoted cell longer than CSV_FIELD_CHARS is skipped to its closing quote, "" and
        all: a line inside it that looks like a DDE formula is text in that cell."""
        text = 'say ""x"", |\n' * 20_000 + "=cmd|' /C calc'!A0\n" + 'and ""y""!'
        content = f'"{text}",City Lab\n'.encode()
        assert upload(user_a_client, note, "list.csv", content).status_code == 201

    @pytest.mark.parametrize(
        "content",
        [
            "a,\"=cmd|' /C calc'\"!A0\n",  # the "!" after the closing quote: one field to csv
            "a,\"=cmd\"|' /C calc'!A0\n",  # the "|" after it
            "a,\"  \"=cmd|' /C calc'!A0\n",  # blanks quoted, the formula after the quote
            'a,"=""x""&cmd|\' /C calc\'!A0"\n',  # a string literal before it
        ],
        ids=["bang-after-quote", "pipe-after-quote", "formula-after-quote", "literal-first"],
    )
    def test_text_after_a_closing_quote_joins_the_field(self, user_a_client, note, content):
        assert refusal(upload(user_a_client, note, "list.csv", content.encode())) == (
            storage.CSV_DDE_REFUSED
        )

    @pytest.mark.parametrize(
        "content",
        [
            # Found by the independent review's differential fuzz against the csv module.
            '\'\'\t"@""=,,"a!"""|!',
            "\"-\r'\"\"\"' ;|!''\t'",
            'a,"=""lit"rest"|x!y\n',  # a string literal open when the field's quote closes
            'a,"=\'nm""x"y\'|cmd!A0\n',  # a quoted name open when it closes
        ],
        ids=["fuzz-1", "fuzz-2", "literal-across-the-close", "name-across-the-close"],
    )
    def test_a_formula_goes_on_after_its_fields_closing_quote(self, content):
        """Python's csv joins the text after a quoted field's closing quote to the field, so a
        string literal or quoted name open at the close goes on after it."""
        assert storage._csv_dde(io.BytesIO(content.encode())) is True
        inside = 'a,"=""lit"rest""|x!y\n'  # there the "|" is inside the literal
        assert storage._csv_dde(io.BytesIO(inside.encode())) is False

    def test_bounded_memory_whatever_the_shape(self):
        """Independent review P3-6: one reading per separator, each a stream through C; a row of
        millions of cells or millions of blank lines costs a chunk of memory, not a row."""
        shapes = [
            "sep=#\nname,city;x\ty#z\n" + "a|!," * 600_000,  # one row, 600,000 cells
            "|!\n" + "\n" * 2_000_000,  # blank lines
            '"' + '""' * 500_000 + '",|!\n',  # one quoted field of quotes
        ]
        for text in shapes:
            data = text.encode()
            tracemalloc.start()
            try:
                found = storage._csv_dde(io.BytesIO(data))
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
            assert found is False
            assert peak < 4 * 1024 * 1024, peak  # the csv module held ~140 MB for the first

    def test_text_files_are_not_read_as_cells(self, user_a_client, note):
        content = b"Call =cmd|' /C calc'!A0 is how DDE looks.\n=cmd|' /C calc'!A0\n"
        assert upload(user_a_client, note, "notes.txt", content).status_code == 201
