"""Expected CPT and the agreed CPT (R104): one rule for both (validation.clean_cpt), through
every API path that writes one. CPT has no defined meaning or unit yet, so it is text: kept
as written (cleaned like any one-line text), never re-spelled, never searched or embedded,
never in audit metadata. docs/pipeline.md#expected-cpt."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from arkray.audit.models import AuditEvent
from arkray.pipeline import selectors, validation
from arkray.pipeline.models import NegotiationPrice, Opportunity
from tests.factories import LeadFactory, OpportunityFactory
from tests.helpers import key_header

from .conftest import opportunities_url, opportunity_url

pytestmark = pytest.mark.django_db

DEAL = {"customer_name": "Asha Diagnostics", "value": "500000", "opportunity_date": "2026-10-05"}
BEFORE = "Rs 1 per test"  # what an edit or a revision starts from


def _post(client: Any, url: str, body: dict[str, Any], **headers: str) -> Any:
    # Raw JSON, so a lone surrogate ("\ud800" escaped) reaches the parser as one.
    return client.post(url, json.dumps(body), content_type="application/json", headers=headers)


def _latest_agreed(opportunity_id: Any) -> str:
    cpt: str = (
        NegotiationPrice.objects.filter(opportunity_id=opportunity_id).latest("id").agreed_cpt
    )
    return cpt


def create_expected(client, user, stages, cpt):
    response = _post(client, opportunities_url(), {**DEAL, "expected_cpt": cpt}, **key_header())
    found = Opportunity.objects.first()
    return response, "expected_cpt", (found.expected_cpt if found else None)


def edit_expected(client, user, stages, cpt):
    opp = OpportunityFactory(lead=LeadFactory(owner=user), expected_cpt=BEFORE)
    body = json.dumps({"version": 1, "expected_cpt": cpt})
    response = client.patch(opportunity_url(opp.pk), body, content_type="application/json")
    return response, "expected_cpt", Opportunity.objects.get(pk=opp.pk).expected_cpt


def create_agreed(client, user, stages, cpt):
    body = {
        **DEAL,
        "stage": str(stages["negotiation"].pk),
        "negotiated_price": "10",
        "agreed_cpt": cpt,
    }
    response = _post(client, opportunities_url(), body, **key_header())
    found = Opportunity.objects.first()
    return response, "agreed_cpt", (_latest_agreed(found.pk) if found else None)


def move_agreed(client, user, stages, cpt):
    opp = OpportunityFactory(lead=LeadFactory(owner=user), stage=stages["proposal"])
    body = {
        "stage": str(stages["negotiation"].pk),
        "version": 1,
        "negotiated_price": "10",
        "agreed_cpt": cpt,
    }
    response = _post(client, opportunity_url(opp.pk, action="move"), body)
    prices = NegotiationPrice.objects.filter(opportunity_id=opp.pk)
    return response, "agreed_cpt", (_latest_agreed(opp.pk) if prices.exists() else None)


def revise_agreed(client, user, stages, cpt):
    opp = OpportunityFactory(lead=LeadFactory(owner=user), stage=stages["proposal"])
    entered = {"stage": str(stages["negotiation"].pk), "version": 1}
    moved = _post(
        client,
        opportunity_url(opp.pk, action="move"),
        {**entered, "negotiated_price": "10", "agreed_cpt": BEFORE},
    )
    assert moved.status_code == 200, moved.content
    body = {"version": 2, "price": "11", "agreed_cpt": cpt}
    response = _post(client, opportunity_url(opp.pk, action="negotiated-prices"), body)
    return response, "agreed_cpt", _latest_agreed(opp.pk)


PATHS = {
    "create-expected": create_expected,
    "edit-expected": edit_expected,
    "create-agreed": create_agreed,
    "move-agreed": move_agreed,
    "revise-agreed": revise_agreed,
}
# What is unchanged after a refused write, per path.
UNCHANGED = {
    "create-expected": None,
    "edit-expected": BEFORE,
    "create-agreed": None,
    "move-agreed": None,
    "revise-agreed": BEFORE,
}

KEPT = {
    # value sent -> value stored
    "100 characters": ("x" * 100, "x" * 100),
    "100 multi-byte": (chr(0x20B9) * 100, chr(0x20B9) * 100),  # ₹: 3 bytes in UTF-8
    "100 astral": (chr(0x1F600) * 100, chr(0x1F600) * 100),  # one code point each
    "blanks trimmed": ("  Rs 18 per test  ", "Rs 18 per test"),
    "lines and tabs joined": ("Rs 18\nper\t\ttest\r\n", "Rs 18 per test"),
    # Indic spelling needs the zero-width joiner: it is the one invisible character allowed.
    "joiner kept": ("क्" + chr(0x200D) + "ष 18", "क्" + chr(0x200D) + "ष 18"),
    "decomposed made NFC": ("Re" + chr(0x0301) + "f 4", "R" + chr(0x00E9) + "f 4"),
    # Stored as typed: rendered as text by the UI (no dangerouslySetInnerHTML anywhere), and
    # nothing exports CPT to a spreadsheet, so these are inert strings.
    "markup": ("<script>alert(1)</script>", "<script>alert(1)</script>"),
    "sql": ("18'; DROP TABLE pipeline_opportunity; --", "18'; DROP TABLE pipeline_opportunity; --"),
    "formula": ("=cmd|' /C calc'!A0", "=cmd|' /C calc'!A0"),
    "a number as text": ("18.50", "18.50"),
}
REFUSED = {
    "101 characters": "x" * 101,
    "101 multi-byte": chr(0x20B9) * 101,
    "nul": "Rs 18" + chr(0),
    "escape": "Rs 18" + chr(0x1B) + "[31m",
    "bidi override": chr(0x202E) + "tset rep 81 sR",
    "bidi isolate": "Rs " + chr(0x2066) + "18" + chr(0x2069),
    "zero-width space": "Rs" + chr(0x200B) + "18",
    "byte-order mark": chr(0xFEFF) + "Rs 18",
    "line separator": "Rs 18" + chr(0x2028) + "per test",
    "tag characters": "Rs 18" + chr(0xE0041),
    "lone surrogate": "Rs 18" + chr(0xD800),
    "int": 18,
    "float": 18.5,
    "bool": True,
    "list": ["Rs 18"],
    "object": {"rs": 18},
}


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", KEPT)
def test_a_cpt_is_kept_as_written(user_a, user_a_client, stages, path, case):
    sent, stored = KEPT[case]
    response, field, kept = PATHS[path](user_a_client, user_a, stages, sent)
    assert response.status_code in (200, 201), response.content
    assert kept == stored
    if path != "revise-agreed":  # the revision answers with the price row
        assert response.json()[field] == stored


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("case", REFUSED)
def test_a_cpt_that_isnt_one_line_of_visible_text_is_refused(
    user_a, user_a_client, stages, path, case
):
    response, field, kept = PATHS[path](user_a_client, user_a, stages, REFUSED[case])
    assert response.status_code == 400, response.content
    assert list(response.json()["error"]["details"]) == [field]
    assert kept == UNCHANGED[path]
    if not isinstance(REFUSED[case], str):
        assert response.json()["error"]["details"][field] == [validation.CPT_NOT_TEXT]


def test_one_rule_and_one_bound_for_both_cpts():
    assert validation.CLEANERS["expected_cpt"] is validation.clean_cpt
    assert validation.CPT_MAX_LENGTH == 100
    assert Opportunity._meta.get_field("expected_cpt").max_length == validation.CPT_MAX_LENGTH
    assert NegotiationPrice._meta.get_field("agreed_cpt").max_length == validation.CPT_MAX_LENGTH
    assert "product-owner confirmation" in (validation.clean_cpt.__doc__ or "")
    # The services check too (imports, tools): a number never becomes text there either.
    for value in (18, 18.5, None, b"Rs 18"):
        with pytest.raises(ValueError, match="as text"):
            validation.clean_cpt(value)


# --- where a CPT never goes ------------------------------------------------------------------
@pytest.fixture
def with_cpts(user_a, user_a_client, stages):
    body = {
        **DEAL,
        "description": "Annual reagent contract",
        "expected_cpt": "Zqvexpected 18",
        "stage": str(stages["negotiation"].pk),
        "negotiated_price": "10",
        "agreed_cpt": "Zqvagreed 17",
    }
    response = _post(user_a_client, opportunities_url(), body, **key_header())
    assert response.status_code == 201, response.content
    return response.json()


def test_search_doesnt_match_a_cpt(user_a_client, with_cpts):
    for q in ("Zqvexpected", "Zqvagreed"):
        response = user_a_client.get(f"/api/v1/workspaces/me/search?q={q}")
        assert response.status_code == 200, response.content
        body = response.json()
        groups = ("leads", "opportunities", "tasks", "meetings", "notes")
        assert all(body[group]["results"] == [] for group in groups), q
    # The customer name of the same opportunity is found: the search itself works.
    found = user_a_client.get("/api/v1/workspaces/me/search?q=Asha").json()
    assert [r["id"] for r in found["opportunities"]["results"]] == [with_cpts["id"]]


def test_the_semantic_index_never_holds_a_cpt(with_cpts):
    (document,) = selectors.knowledge_documents_for_indexing([uuid.UUID(with_cpts["id"])])
    assert "Annual reagent contract" in document.text
    assert "Zqv" not in document.text
    assert "Zqv" not in document.label


def test_audit_names_the_cpt_field_never_its_text(user_a, user_a_client, with_cpts):
    opp_id = with_cpts["id"]
    response = user_a_client.patch(
        opportunity_url(opp_id), {"version": 1, "expected_cpt": "Meera rate 4.5"}, format="json"
    )
    assert response.status_code == 200, response.content
    updated = AuditEvent.objects.get(action="opportunity.updated", target_id=opp_id)
    assert updated.metadata["fields"] == ["expected_cpt"]
    for event in AuditEvent.objects.filter(target_id=opp_id):
        recorded = json.dumps(event.metadata)
        assert "Zqv" not in recorded
        assert "Meera" not in recorded
