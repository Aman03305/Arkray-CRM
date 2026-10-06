"""Regressions for the final whole-software audit: each test reproduces a finding and pins its fix
(finding ids refer to the audit report)."""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings
from django.utils import timezone

from arkray.ai import answers, formatting, tools
from arkray.ai.tools import ToolContext
from arkray.core.access import AccessScope
from arkray.pipeline.models import Pipeline, Stage
from tests.factories import LeadFactory, OpportunityFactory

# --- RAG-1: redaction runs in linear time ------------------------------------------------------
ADVERSARIAL_NOTES = {
    "dotted label chain": "a." * 5000,
    "hyphenated label": "a-" * 5000,
    "one long word": "a" * 20000,
    "email-shaped run": "a.b+c" * 2000,
    "digit run": "9" * 10000,
    "dash digits": "6-" * 5000,
    "unicode labels": "é." * 5000,
}


@pytest.mark.parametrize("name", ADVERSARIAL_NOTES)
def test_redaction_is_linear_on_crafted_text(name):
    """One 10,000-character note used to hold an AI worker for 10 s (quadratic matching)."""
    text = ADVERSARIAL_NOTES[name]
    started = time.perf_counter()
    formatting.user_text(text)
    formatting.user_text_slice(text, 0, len(text) // 2)
    assert time.perf_counter() - started < 0.5  # about 5 ms; 1 s or more before the fix


def test_redaction_still_finds_links_emails_and_phones():
    text = (
        "Join zoom.us/j/123?pwd=SECRET1 or (meet.google.com/abc-defg-hij),"
        " mail dr.mehta@apollo.example, https://x.example/t?k=SECRET2,"
        " www.drive.example/d, call +91 98765 43210."
    )
    redacted = formatting.user_text(text)
    for secret in ("SECRET1", "SECRET2", "abc-defg-hij", "mehta", "98765", "drive.example"):
        assert secret not in redacted, secret
    assert redacted.count("[link]") == 4
    assert "[email]" in redacted
    assert "[phone]" in redacted
    kept = "PO 7,77,77,777; Rs. 5,00,000; 12.5 units; 2026-10-04; e.g. apollo.example"
    assert formatting.user_text(kept) == kept


# --- RAG-2: a CPT the CRM holds may be repeated as written -------------------------------------
def test_the_models_repeat_of_a_recorded_cpt_is_grounded():
    tool_result = json.dumps(
        {
            "latest_agreed_cpt": "Rs 18 per test",
            "history": [{"agreed_cpt": "Rs 19 per test"}],
            "expected_cpt": "₹17.50",
        }
    )
    allowed = answers.allowed_money([tool_result], "latest price?")
    narrative = "The latest agreed CPT is Rs 18 per test (earlier Rs 19, expected ₹17.50)."
    assert answers.ungrounded_money(narrative, allowed) == set()


def test_other_user_text_still_grounds_no_money():
    """Only the CPT fields state a rate; a number in a note is not money the CRM computed."""
    tool_result = json.dumps({"passages": [{"text": "PO 7,77,77,777 and Rs 777 were discussed"}]})
    allowed = answers.allowed_money([tool_result], "q")
    assert answers.ungrounded_money("The pipeline is worth ₹7,77,77,777.", allowed) == {
        Decimal(77777777)
    }


# --- DBPERF-1: the organisation-wide stage breakdown is bounded --------------------------------
@pytest.mark.django_db
def test_the_all_pipelines_summary_is_bounded_whatever_the_number_of_pipelines(
    user_a, django_assert_max_num_queries
):
    """Above ~35 pipelines holding deals it used to exceed the tool-output budget and fail the
    whole question with HTTP 500; its query count also grew with the pipeline count."""
    lead = LeadFactory(owner=user_a)
    total = Decimal(0)
    for number in range(24):
        pipeline = Pipeline.objects.create(
            key=f"p{number:03d}", name=f"Pipeline {number}", owner=user_a
        )
        stages = [
            Stage.objects.create(
                pipeline=pipeline,
                key=f"s{k}",
                name=f"Stage {k}",
                position=k * 10,
                probability=Decimal(10 * k),
                category="open",
            )
            for k in range(1, 7)
        ]
        value = Decimal(1000 * (number + 1))
        total += value
        OpportunityFactory(lead=lead, owner=user_a, stage=stages[0], value=value)
    ctx = ToolContext(scope=AccessScope.own(user_a.pk), now=timezone.now())
    with django_assert_max_num_queries(40):
        outcome = tools.execute(ctx, "get_pipeline_summary", {})
    assert not outcome.is_error, outcome.content
    assert len(outcome.content) < settings.AI_TOOL_RESULTS_MAX_CHARS
    body: dict[str, Any] = json.loads(outcome.content)
    assert body["pipeline_value"]["amount"] == str(
        total.quantize(Decimal("0.01"))
    )  # every pipeline
    assert len(body["by_stage"]) <= tools.MAX_PIPELINES_LISTED
    assert body["more_pipelines"] >= 14
    # The largest open value is listed first.
    assert body["by_stage"][0]["pipeline"] == "Pipeline 23"


@pytest.mark.django_db
def test_a_few_pipelines_are_all_listed(user_a):
    lead = LeadFactory(owner=user_a)
    OpportunityFactory(lead=lead, owner=user_a)
    ctx = ToolContext(scope=AccessScope.own(user_a.pk), now=timezone.now())
    body = json.loads(tools.execute(ctx, "get_pipeline_summary", {}).content)
    assert "more_pipelines" not in body
    assert body["by_stage"]
