"""A correction to a maximal name (audit DOMAIN-1; pipeline.corrections).

A lead's first and last names are each at most 100 characters, so its full name can be 201;
a deal's customer and account copies hold 200. The copy takes the name cut to 200, as a deal
created for an existing lead does (leads.selectors.customer_names), never a 500, and a later
correction still recognises the cut copy and follows.
"""

from __future__ import annotations

import pytest

from arkray.core.access import AccessScope
from arkray.pipeline import corrections
from tests.factories import LeadFactory, OpportunityFactory

pytestmark = pytest.mark.django_db

FIRST, LAST = "A" * 100, "B" * 100
FULL = f"{FIRST} {LAST}"  # 201 characters


def correct(lead, **changes):
    lead.refresh_from_db()
    return corrections.correct_customer(
        actor=lead.owner,
        scope=AccessScope.own(lead.owner_id),
        lead_id=lead.pk,
        version=lead.version,
        changes=changes,
    )


def test_the_copy_takes_the_name_cut_to_its_length_and_follows_later(user_a):
    lead = LeadFactory(owner=user_a, first_name="Asha", last_name="Mehta")
    deal = OpportunityFactory(lead=lead, customer_name="Asha Mehta", account_name="Apollo Labs")
    result = correct(lead, first_name=FIRST, last_name=LAST)
    assert result.opportunities == 1
    deal.refresh_from_db()
    assert deal.customer_name == FULL[:200]
    assert len(deal.title) <= 200
    assert deal.account_name == "Apollo Labs"  # a deliberate organisation stays
    correct(lead, first_name="Asha", last_name="Mehta")
    deal.refresh_from_db()
    assert deal.customer_name == "Asha Mehta"  # the cut copy was recognised


def test_a_person_only_leads_account_copy_is_cut_too(user_a):
    lead = LeadFactory(owner=user_a, first_name="Asha", last_name="Mehta", organization_name="")
    deal = OpportunityFactory(lead=lead, customer_name="Asha Mehta", account_name="Asha Mehta")
    correct(lead, first_name=FIRST, last_name=LAST)
    deal.refresh_from_db()
    assert deal.customer_name == deal.account_name == FULL[:200]
    correct(lead, last_name="Rao")
    deal.refresh_from_db()
    assert deal.customer_name == deal.account_name == f"{FIRST} Rao"
