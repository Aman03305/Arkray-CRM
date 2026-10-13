"""Previews are masked on the whole text, then cut (adversarial review M5).

Lists and search results carry a bounded preview of a note's or a task's text. It used to be
cut by PostgreSQL first (`find_records`: 240 characters from 60 before the first search word;
the lists: the first 240) and masked as a fragment: a meeting link cut after its host went out
as `456789?pwd=<passcode>`, a phone number cut in two as two harmless-looking halves. The
tools now read the whole text, mask it, and only then cut it (formatting.clip), so whatever
the cut, a link, a number or a passcode is replaced whole or not at all.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

import pytest
from django.utils import timezone

from arkray.activities.selectors import PREVIEW_CONTEXT, PREVIEW_LENGTH
from arkray.ai import tools
from arkray.ai.tools import ToolContext
from arkray.core.access import AccessScope
from arkray.pipeline.models import Pipeline, Stage
from tests.ai_fixtures import own
from tests.factories import LeadFactory, NoteFactory, OpportunityFactory, TaskFactory

pytestmark = pytest.mark.django_db

# Built at run time: no source line pairs a credential word with a value (secret scanning).
HIDDEN = "Wren7" + "Canary42"
TERM = "escalation"


def ok(scope: AccessScope, name: str, args: dict[str, Any] | None = None) -> Any:
    ctx = ToolContext(scope=scope, now=timezone.now())
    outcome = tools.execute(ctx, name, args or {})
    body = json.loads(outcome.content)
    assert not outcome.is_error, body
    return body


def fragments(text: str, secret: str, size: int = 4) -> list[str]:
    """Every piece of `secret` (`size` characters or more) that `text` still carries."""
    return [
        secret[i : i + size] for i in range(len(secret) - size + 1) if secret[i : i + size] in text
    ]


def preview_starting_at(head: str, cut: int, tail: str = " with the lab team") -> str:
    """A note whose search preview starts exactly at `cut`: the first search word sits
    PREVIEW_CONTEXT characters after it (activities.selectors.search)."""
    gap = cut + PREVIEW_CONTEXT - len(head) - 1
    assert gap >= 1, "the head is too long for the cut"
    text = f"{head} {('then ' * gap)[:gap]}{TERM}{tail}"
    assert text.upper().index(TERM.upper()) - PREVIEW_CONTEXT == cut
    return text


def found_note(user: Any, text: str) -> Any:
    NoteFactory(lead=LeadFactory(owner=user), description=text)
    body = ok(own(user), "find_records", {"query": TERM})
    (row,) = body["notes"]
    return row


class TestFindRecords:
    def test_a_passcode_in_a_link_the_preview_cuts_never_leaves(self, user_a):
        head = f"Zoom bridge https://us02web.zoom.us/j/88123456789?pwd={HIDDEN}"
        cut = head.index("/j/") + len("/j/88123")
        text = preview_starting_at(head, cut)
        assert text[cut:].startswith(f"456789?pwd={HIDDEN}")  # the reviewer's fragment
        row = found_note(user_a, text)
        sent = json.dumps(row)
        assert fragments(sent, HIDDEN) == []
        assert "88123456789"[-4:] not in sent
        assert row["untrusted_text"]["text"].startswith("[link]")
        assert row["untrusted_text"]["truncated"] is True

    def test_a_phone_number_the_preview_cuts_at_its_start_is_replaced_whole(self, user_a):
        head = "Ring Sunil on 98111 55555"
        cut = head.index("98111") + 2  # the preview starts at "111 55555"
        row = found_note(user_a, preview_starting_at(head, cut))
        shown = row["untrusted_text"]["text"]
        assert shown.startswith("[phone]")
        assert not re.search(r"\d", json.dumps(row["untrusted_text"])), shown

    def test_a_phone_number_the_preview_cuts_at_its_end_is_replaced_whole(self, user_a):
        lead_in = f"{TERM} call: "
        text = lead_in + "x" * (PREVIEW_LENGTH - len(lead_in) - 7) + " +91 98111 55555 later"
        assert text[PREVIEW_LENGTH - 6 : PREVIEW_LENGTH + 1].strip().startswith("+91")
        row = found_note(user_a, text)
        shown = row["untrusted_text"]["text"]
        assert shown.endswith("[phone]"), shown
        assert not re.search(r"\d", shown), shown

    def test_a_passcode_written_out_is_masked_wherever_the_preview_starts(self, user_a):
        head = "Teams call. Meeting ID 4402 8812; Passcode: 482913"
        cut = head.index("Passcode") + 4  # the preview starts at "code: 482913"
        row = found_note(user_a, preview_starting_at(head, cut))
        shown = row["untrusted_text"]["text"]
        assert "482913" not in shown, shown
        assert "4829" not in shown, shown
        assert "[secret]" in shown

    def test_alternate_phone_shapes_in_a_note_are_masked(self, user_a):
        text = (
            f"{TERM}: Mob.9811155555, Ph:+91.98111.55555, alt 98111/55555 or "
            "98111_55555, office (022) 2345-6789, home 0124 234 5678"
        )
        row = found_note(user_a, text)
        shown = row["untrusted_text"]["text"]
        assert not re.search(r"\d", shown), shown
        assert shown.count("[phone]") == 6


class TestListPreviews:
    def test_a_task_preview_cut_inside_a_phone_number_is_replaced_whole(self, user_a):
        lead = LeadFactory(owner=user_a)
        text = "Call back " + "x" * (PREVIEW_LENGTH - 16) + " 98111 55555 tomorrow"
        assert re.search(r"\d", text[: PREVIEW_LENGTH + 1])  # PostgreSQL's cut is inside it
        TaskFactory(lead=lead, description=text)
        (task,) = ok(own(user_a), "list_tasks", {"filter": "open"})["tasks"]
        shown = task["untrusted_text"]["text"]
        assert shown.endswith("[phone]"), shown
        assert task["untrusted_text"]["truncated"] is True
        assert not re.search(r"\d", shown)

    def test_a_note_preview_in_a_record_cut_inside_a_passcode_is_replaced_whole(self, user_a):
        lead = LeadFactory(owner=user_a)
        lead_in = "Dial in: "
        text = lead_in + "y" * (PREVIEW_LENGTH - len(lead_in) - 13) + " Passcode: 482913 at 4"
        assert text[: PREVIEW_LENGTH + 1].endswith("Passcode: 482")  # PostgreSQL's cut
        NoteFactory(lead=lead, description=text)
        body = ok(own(user_a), "get_record", {"ref": f"lead:{lead.pk}"})
        (activity,) = body["recent_activities"]
        shown = activity["untrusted_text"]["text"]
        assert shown.endswith("Passcode: [secret]"), shown
        assert not re.search(r"\d", shown), shown

    def test_a_link_cut_at_the_end_of_a_preview_goes_as_a_whole_link(self, user_a):
        """Safe before too (the host survives an end cut); kept so it stays so."""
        lead = LeadFactory(owner=user_a)
        lead_in = "Dial in: "
        text = (
            lead_in
            + "y" * (PREVIEW_LENGTH - len(lead_in) - 30)
            + (f" zoom.us/j/88123456789?pwd={HIDDEN} for the review")
        )
        NoteFactory(lead=lead, description=text)
        body = ok(own(user_a), "get_record", {"ref": f"lead:{lead.pk}"})
        (activity,) = body["recent_activities"]
        assert fragments(json.dumps(activity), HIDDEN) == []
        assert "[link]" in activity["untrusted_text"]["text"]

    def test_a_preview_whose_record_is_no_longer_visible_is_left_out(self, user_a, user_b):
        """The whole text is re-read through the scope: a record that left it between the
        two reads (reassigned) has no preview, never the database's unmasked fragment."""
        mine = NoteFactory(lead=LeadFactory(owner=user_a), description="Mine, 98111 55555.")
        theirs = NoteFactory(lead=LeadFactory(owner=user_b), description="Theirs.")
        quiet = NoteFactory(lead=LeadFactory(owner=user_a), description="No preview asked.")
        for row in (mine, theirs):
            row.text_preview = row.description[:10]  # as a list query annotates it
        ctx = ToolContext(scope=own(user_a), now=timezone.now())
        texts = tools.full_texts(ctx, [mine, theirs, quiet])
        assert texts == {mine.pk: "Mine, 98111 55555."}
        row = tools._activity_row(ctx, theirs, texts)
        assert "untrusted_text" not in row  # never the database's fragment


class TestNamesTypedByUsers:
    def test_pipeline_and_stage_names_are_masked(self, user_a):
        """Pipeline and stage names are typed by users too (hardening, adversarial review)."""
        pipeline = Pipeline.objects.create(
            key="ptyped", name="Clinics +91.98111.55555", owner=user_a
        )
        stage = Stage.objects.create(
            pipeline=pipeline,
            key="s1",
            name="Call 9811155555",
            position=10,
            probability=Decimal("20"),
            category="open",
        )
        OpportunityFactory(lead=LeadFactory(owner=user_a), stage=stage, value=Decimal("100000"))
        summary = json.dumps(ok(own(user_a), "get_pipeline_summary"))
        listed = json.dumps(ok(own(user_a), "list_opportunities"))
        for sent in (summary, listed):
            assert "98111" not in sent, sent
            assert "55555" not in sent, sent
            assert "[phone]" in sent
        ctx = ToolContext(scope=own(user_a), now=timezone.now())
        refused = tools.execute(ctx, "list_opportunities", {"stage": "Nonexistent"})
        assert refused.is_error
        assert "98111" not in refused.content
