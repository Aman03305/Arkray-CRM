"""A customer's data export in bounded memory (review of 73ff1e3, M6; privacy.exports;
docs/privacy.md#access-requests).

The export loaded every activity of the customer, full text included, then built the whole
JSON and every CSV as strings in memory before writing the ZIP: a customer with tens of
thousands of long notes (which nothing capped) could make the build run out of memory or
time, and the data-rights request fail. Now the records are read in bounded batches and
written straight into the ZIP spooled on disk; memory doesn't grow with the customer.

And a spreadsheet cell that starts with spaces before a formula (`"  =HYPERLINK(...)"`) is
neutralised like one that starts with the formula (review, Low: the check missed it).

Review round 1 (QUEUES-1): the size cap counted the uncompressed JSON and CSV, so a customer
within the activity budget (5,000 notes of 10,000 characters: 104.5 of 104.9 MB before their
timeline; Devanagari from about 1,929 notes) or one deal moved between stages for six hours
could no longer be exported. The cap now bounds the archive as it is stored (compressed), which
is also what the worker's temporary space, the storage and the edge's download spool hold.
"""

from __future__ import annotations

import csv
import io
import json
import random
import tracemalloc
import zipfile
from typing import Any

import pytest
from django.core.files.storage import storages

from arkray.activities import services as activity_services
from arkray.activities.models import DESCRIPTION_MAX_LENGTH, Activity
from arkray.core.access import AccessScope
from arkray.pipeline import services as pipeline_services
from arkray.pipeline.models import Opportunity
from arkray.privacy import exports
from arkray.privacy.models import DataExport
from tests.factories import (
    LeadFactory,
    NoteFactory,
    OpportunityFactory,
    TaskFactory,
    default_stage,
)

pytestmark = pytest.mark.django_db

NOTES = 1500
NOTE_LENGTH = 8000
FORMULA = '=HYPERLINK("http://evil.example","click")'
# Words of a sales visit's notes in Hindi, in random order (seeded): non-ASCII text without the
# repetition of one sentence pasted over and over, which compresses unrealistically well.
HINDI_TEXT = (
    "प्रयोगशाला प्रबंधक के साथ अभिकर्मक आपूर्ति पर चर्चा हुई और उन्होंने अगले महीने नई मशीन "
    "का प्रदर्शन मांगा डॉक्टर मेहता ने कहा कि मरीजों की संख्या बढ़ रही है इसलिए जांच तेज होनी "
    "चाहिए हमने कीमत भेजी उन्होंने छूट के बारे में पूछा अस्पताल का बजट मार्च में तय होगा खरीद "
    "विभाग से बात करनी है रिपोर्ट समय पर नहीं मिलती रक्त शर्करा की जांच के लिए किट चाहिए नमूने "
    "सुबह आते हैं तकनीशियन प्रशिक्षण चाहते हैं सेवा अनुबंध एक साल का हो बिजली कटौती से समस्या "
    "होती है बैकअप की जरूरत है अगली बैठक सोमवार को होगी निदेशक भी मौजूद रहेंगे गुणवत्ता नियंत्रण "
    "के नियम बदले हैं नई दरें लागू होंगी भुगतान तीस दिन में होगा पुरानी मशीन बदलनी है"
)
HINDI = HINDI_TEXT.split()


def hindi_note(rng: random.Random) -> str:
    """A note of the maximum length, sentences of random words."""
    out: list[str] = []
    size = 0
    while size < DESCRIPTION_MAX_LENGTH:
        sentence = (
            " ".join(rng.choice(HINDI) for _ in range(rng.randint(6, 14))) + chr(0x0964) + " "
        )
        out.append(sentence)
        size += len(sentence)
    return "".join(out)[:DESCRIPTION_MAX_LENGTH].strip()


def export_of(admin, lead) -> DataExport:
    return DataExport.objects.create(
        subject_type="lead", subject_id=lead.pk, requested_by=admin, reference="DSAR-2026-17"
    )


def opened(key: str) -> zipfile.ZipFile:
    with storages["exports"].open(key, "rb") as stored:
        return zipfile.ZipFile(io.BytesIO(stored.read()))


def csv_rows(archive: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(archive.read(f"csv/{name}.csv").decode("utf-8-sig"))))


def customer(admin, owner, notes: int, body: str) -> DataExport:
    """A customer with `notes` long notes (half of them on a deal), written directly as years
    of use would leave them, and an export of them to build."""
    lead = LeadFactory(owner=owner, first_name="Sunita", last_name="Rao")
    deal = OpportunityFactory(lead=lead)
    Activity.objects.bulk_create(
        [
            *NoteFactory.build_batch(notes // 2, lead=lead, description=body),
            *NoteFactory.build_batch(
                notes - notes // 2, lead=lead, opportunity=deal, description=body
            ),
        ],
        batch_size=500,
    )
    return export_of(admin, lead)


def peak_of_build(export: DataExport) -> tuple[int, str]:
    tracemalloc.start()
    try:
        key, *_ = exports.build(export)
        return tracemalloc.get_traced_memory()[1], key
    finally:
        tracemalloc.stop()


class TestBoundedMemory:
    def test_peak_memory_does_not_grow_with_the_customers_notes(self, admin, user_a):
        body = ("The lab manager discussed reagent supply in detail. " * 200)[:NOTE_LENGTH]
        small, _ = peak_of_build(customer(admin, user_a, NOTES // 5, body))
        large, key = peak_of_build(customer(admin, user_a, NOTES, body))
        text = NOTES * NOTE_LENGTH  # about 12 MB of notes
        added = text - NOTES // 5 * NOTE_LENGTH
        # Everything in memory at once peaked at several times the text (the rows, the JSON
        # string and its bytes, each CSV and its bytes: 82 MB for these 12 MB). Now one batch
        # of rows at a time: what grows is the ids (what the conversations are matched by).
        assert large - small < added / 5, f"{small / 1e6:.1f} MB, then {large / 1e6:.1f} MB"
        assert large < text / 2, f"peak {large / 1e6:.1f} MB for {text / 1e6:.1f} MB of notes"
        archive = opened(key)
        data = json.loads(archive.read("data.json"))
        assert len(data["activities"]) == NOTES
        assert {a["description"] for a in data["activities"]} == {body}
        assert len(csv_rows(archive, "activities")) == NOTES
        created = [a["created_at"] for a in data["activities"]]
        assert created == sorted(created)  # oldest first, as before

    def test_every_section_and_table_is_still_there_in_full(self, admin, user_a):
        lead = LeadFactory(
            owner=user_a, first_name="Sunita", last_name="Rao", email="s@lab.example"
        )
        deal = OpportunityFactory(lead=lead, customer_name="Sunita Rao")
        NoteFactory(lead=lead, opportunity=deal, description="Asked for a demo.")
        TaskFactory(lead=lead, title="Send the quotation")
        export = export_of(admin, lead)
        key, *_ = exports.build(export)
        archive = opened(key)
        raw = archive.read("data.json")
        data = json.loads(raw)
        # Written piece by piece, byte for byte what json.dumps made of the whole document.
        assert raw == json.dumps(data, indent=2, ensure_ascii=False).encode()
        whole, _ = exports.lead_document(lead.pk)
        assert {name: value for name, value in data.items() if name != "export"} == json.loads(
            json.dumps(whole, default=str)
        )
        assert list(data) == [
            "export",
            "lead",
            "opportunities",
            "stage_history",
            "negotiated_prices",
            "deal_corrections",
            "activities",
            "attachments",
            "timeline",
            "ask_arkray",
        ]
        assert data["lead"]["email"] == "s@lab.example"
        assert data["opportunities"][0]["customer_name"] == "Sunita Rao"
        assert sorted(a["type"] for a in data["activities"]) == ["note", "task"]
        assert data["export"]["reference"] == "DSAR-2026-17"
        names = set(archive.namelist())
        for table in (
            "lead",
            "opportunities",
            "stage_history",
            "negotiated_prices",
            "deal_corrections",
            "activities",
            "attachments",
            "ask_arkray",
        ):
            assert f"csv/{table}.csv" in names
        assert csv_rows(archive, "lead")[0]["email"] == "s@lab.example"
        assert "README.txt" in names

    def test_an_export_over_the_size_cap_stops_early_and_leaves_nothing(
        self, admin, user_a, settings
    ):
        """The cap counts the archive as stored (compressed): text that doesn't compress
        (random letters) is what can still exceed it."""
        settings.EXPORT_MAX_BYTES = 50_000
        lead = LeadFactory(owner=user_a)
        rng = random.Random(3)
        letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        Activity.objects.bulk_create(
            [
                NoteFactory.build(lead=lead, description="".join(rng.choices(letters, k=2000)))
                for _ in range(100)
            ],
            batch_size=100,
        )
        export = export_of(admin, lead)
        with pytest.raises(exports.ExportTooLarge):
            exports.build(export)
        assert not storages["exports"].exists(exports.storage_key(export))


class TestAtTheBudget:
    """Review round 1 (QUEUES-1): any customer within the budgets can be exported. At 1/100
    of the defaults (an affordable test; the cap scales with the budget): ACTIVITY_MAX_PER_
    CUSTOMER notes of the maximum length, in Hindi, created through the service (their timeline
    entries and audit events with them), against the same share of EXPORT_MAX_BYTES."""

    SCALE = 100

    def test_a_customer_full_of_maximum_length_non_ascii_notes_is_exported(
        self, settings, admin, user_a
    ):
        budget = settings.ACTIVITY_MAX_PER_CUSTOMER // self.SCALE
        settings.EXPORT_MAX_BYTES //= self.SCALE  # about 1 MB for 50 notes
        settings.ACTIVITY_MAX_PER_CUSTOMER = budget
        lead = LeadFactory(owner=user_a, first_name="Sunita", last_name="Rao")
        rng = random.Random(7)
        scope = AccessScope.own(user_a.pk)
        bodies = [hindi_note(rng) for _ in range(budget)]
        for body in bodies:
            activity_services.create_activity(
                actor=user_a,
                scope=scope,
                activity_type="note",
                fields={"description": body},
                lead_id=lead.pk,
            )
        with pytest.raises(activity_services.ActivityLimitReached):
            activity_services.create_activity(
                actor=user_a,
                scope=scope,
                activity_type="note",
                fields={"description": "One more."},
                lead_id=lead.pk,
            )
        export = export_of(admin, lead)
        exports.run(export.pk)
        export.refresh_from_db()
        assert (export.status, export.error_code) == ("ready", ""), export.error_code
        assert export.size <= settings.EXPORT_MAX_BYTES
        data = json.loads(opened(export.storage_key).read("data.json"))
        assert sorted(a["description"] for a in data["activities"]) == sorted(bodies)
        assert len(data["timeline"]) >= budget  # one "note added" entry each, at least

    def test_a_deals_long_stage_history_is_exported(self, settings, admin, user_a):
        """Stage moves aren't budgeted: 200 moves of one deal, whose history (with the
        timeline) is more than the cap uncompressed and a fraction of it compressed."""
        lead = LeadFactory(owner=user_a)
        deal: Any = OpportunityFactory(lead=lead)
        scope = AccessScope.own(user_a.pk)
        stages = [default_stage("qualified").pk, default_stage("proposal").pk]
        for n in range(200):
            pipeline_services.move_opportunity(
                actor=user_a,
                scope=scope,
                opportunity_id=deal.pk,
                version=Opportunity.objects.get(pk=deal.pk).version,
                stage_id=stages[n % 2],
            )
        settings.EXPORT_MAX_BYTES = 60_000  # the history alone is about 90 kB uncompressed
        export = export_of(admin, lead)
        exports.run(export.pk)
        export.refresh_from_db()
        assert (export.status, export.error_code) == ("ready", ""), export.error_code
        data = json.loads(opened(export.storage_key).read("data.json"))
        assert len(data["stage_history"]) >= 200
        moved = data["stage_history"][-1]
        assert {"expected_close_date", "opportunity_date"} <= set(moved)  # what history holds
        (row, *_) = csv_rows(opened(export.storage_key), "stage_history")
        assert {"expected_close_date", "opportunity_date"} <= set(row)


class TestFormulaCells:
    @pytest.mark.parametrize(
        "title",
        [
            "  " + FORMULA,
            "\t" + FORMULA,
            chr(0xA0) + FORMULA,  # a no-break space
            "\n" + FORMULA,
            " +1+1",
            " -1",
            " @SUM(A1)",
        ],
        ids=["spaces", "tab", "nbsp", "newline", "plus", "minus", "at"],
    )
    def test_leading_whitespace_does_not_hide_a_formula(self, admin, user_a, title):
        lead = LeadFactory(owner=user_a)
        TaskFactory(lead=lead, title=title)
        key, *_ = exports.build(export_of(admin, lead))
        (row,) = csv_rows(opened(key), "activities")
        assert row["title"] == "'" + title

    def test_ordinary_text_is_left_alone(self, admin, user_a):
        lead = LeadFactory(owner=user_a)
        TaskFactory(lead=lead, title="  Call back = soon")
        key, *_ = exports.build(export_of(admin, lead))
        (row,) = csv_rows(opened(key), "activities")
        assert row["title"] == "  Call back = soon"

    @pytest.mark.parametrize(
        ("prefix", "start"),
        [
            (chr(0x200B), "="),  # zero-width space
            (chr(0xFEFF), "="),  # byte order mark / zero-width no-break space
            (chr(0x2060), "="),  # word joiner
            (chr(0x180E), "="),  # Mongolian vowel separator
            (chr(0x200E), "="),  # left-to-right mark
            (chr(0x00AD), "="),  # soft hyphen
            (chr(0x200B) + " " + chr(0x200E), "+"),  # mixed invisible and blank
            ("", chr(0xFF1D)),  # full-width equals sign
            ("", chr(0xFE66)),  # small equals sign
            ("", chr(0xFF0B)),  # full-width plus sign
            ("", chr(0xFF20)),  # full-width commercial at
            ("", chr(0x2212)),  # minus sign
            ("", chr(0xFF0D)),  # full-width hyphen-minus
            (chr(0x200B), chr(0xFF1D)),
        ],
        ids=[
            "zwsp",
            "bom",
            "word-joiner",
            "mongolian-vs",
            "lrm",
            "soft-hyphen",
            "mixed",
            "fullwidth-equals",
            "small-equals",
            "fullwidth-plus",
            "fullwidth-at",
            "minus-sign",
            "fullwidth-minus",
            "zwsp-fullwidth-equals",
        ],
    )
    def test_invisible_characters_and_lookalikes_do_not_hide_a_formula(self, prefix, start):
        """Review round 1 (QUEUES-6): characters a spreadsheet may drop or fold before it reads
        the cell (format characters, compatibility forms of = + - @, the minus sign)."""
        text = prefix + start + 'HYPERLINK("http://evil.example","click")'
        assert exports._cell(text) == "'" + text

    @pytest.mark.parametrize(
        "text",
        [
            "Called " + chr(0x200B) + "= agreed",
            chr(0x200B) + "Agreed the price",
            "5" + chr(0x2212) + "3 units",
            chr(0x0939) + chr(0x093F) + chr(0x0928) + chr(0x094D) + chr(0x0926) + chr(0x0940),
        ],
        ids=["equals-inside", "invisible-then-text", "minus-inside", "devanagari"],
    )
    def test_text_that_starts_with_no_formula_is_left_alone(self, text):
        assert exports._cell(text) == text


def test_a_build_may_run_longer_than_the_workers_default():
    """Review round 1: a customer at the budgets builds in about 30 s; the job gets 240 s
    (Celery's default is 100 s), within the outbox's running lease."""
    from arkray.core import outbox

    spec = outbox._HANDLERS[exports.TOPIC_BUILD]
    assert spec.time_limit == exports.EXPORT_TIME_LIMIT_S == 240
    assert spec.time_limit + outbox.TIME_LIMIT_GRACE < outbox.settings.OUTBOX_LEASE_SECONDS
