"""What a viewer may see of a deal's customer (docs/authorization.md#historical-deals,
privacy remediation P2-10).

An opportunity carries a copy of its customer (ADR-0028): name, phone, email, address. When
its lead is reassigned, its open deals follow the lead, but a closed one stays with the
salesperson who worked it: their sales history. That person no longer looks after the
customer, so the deal is shown to them without the customer's identity and contact details:

- the customer name, contact phone, contact email and address are blank;
- the deal's name is rebuilt from what isn't personal: the account (the organisation), and
  the instrument; when the account is the person's own name (a lead that is just a person),
  "Customer restricted" stands in for it;
- custom values of free-text fields are left out (they may hold anything about the
  customer); numbers, amounts, dates, yes/no and choices stay;
- `customer_restricted` says so, for the screen to explain it.

Everything commercial stays: value, stages, prices, CPTs, dates, the deal's own notes. A
viewer who sees the lead (its owner now, an administrator organisation-wide) sees it all.
The same rule applies wherever a deal is shown: the pipeline's lists, boards and pages,
global search (which matches such a deal by its organisation and instrument only), the
activities' deal links, Ask Arkray's tools (named the same way, citations included) and its
retrieval (which never returns such a deal's text); a support session sees exactly what its
user sees. A product decision recorded as
conservative by default (docs/adr/0032-privacy-remediation.md); an administrator can always
see the full record.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from arkray.core.access import AccessScope

from . import naming
from .models import FieldType, Opportunity

RESTRICTED_CUSTOMER = "Customer restricted"
CONTACT_FIELDS = ("customer_name", "contact_phone", "contact_email", "address")
FREE_TEXT_TYPES = frozenset({FieldType.TEXT, FieldType.LONG_TEXT})


def visible(scope: AccessScope, lead_owner_id: UUID) -> bool:
    """Whether the viewer may see the deal's customer: they can see its lead."""
    return scope.permits_owner(lead_owner_id)


def shown_account(opportunity: Opportunity) -> str:
    """The account as a restricted viewer sees it: the organisation, never a person's name
    copied into it (a person-only lead's account is its name)."""
    account = opportunity.account_name.strip()
    if not account or account.casefold() == opportunity.customer_name.strip().casefold():
        return ""
    return account


def shown_title(opportunity: Opportunity) -> str:
    """The deal's name without its customer (the account and instrument only)."""
    return naming.opportunity_title(
        customer_name="",
        account_name=shown_account(opportunity) or RESTRICTED_CUSTOMER,
        instrument_name=opportunity.instrument_name,
    )


def title_for(opportunity: Opportunity, scope: AccessScope, lead_owner_id: UUID) -> str:
    return opportunity.title if visible(scope, lead_owner_id) else shown_title(opportunity)


def restrict(
    data: dict[str, Any], opportunity: Opportunity, free_text_field_ids: set[str] | None = None
) -> dict[str, Any]:
    """A serialised opportunity as a viewer who may not see its customer gets it."""
    data["title"] = shown_title(opportunity)
    if "account_name" in data:
        data["account_name"] = shown_account(opportunity)
    for field in CONTACT_FIELDS:
        if field in data:
            data[field] = ""
    if "custom_fields" in data and isinstance(data["custom_fields"], dict):
        # Unknown types (a caller that didn't say): leave out every value.
        hidden = (
            free_text_field_ids if free_text_field_ids is not None else set(data["custom_fields"])
        )
        data["custom_fields"] = {
            key: value for key, value in data["custom_fields"].items() if key not in hidden
        }
    data["customer_restricted"] = True
    return data
