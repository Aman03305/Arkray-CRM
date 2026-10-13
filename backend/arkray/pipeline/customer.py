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
- the deal's other free text is blank too (`WITHHELD_TEXT`: description, lost reason, work
  load, Expected CPT, the latest agreed CPT), and so is the free text of its histories
  (`WITHHELD_HISTORY_TEXT`: the stage history's lost reasons, the agreed prices' CPTs, the
  reasons of its corrections): it was written about the customer, and a dictated note or an
  administrator's edit names them as readily as a contact field does (adversarial review of
  73ff1e3, L6: the API returned it verbatim while Ask Arkray withheld it);
- `customer_restricted` says so, for the screen to explain it.

Everything commercial stays: value, probability, stages, agreed prices, dates, who moved,
priced and corrected it, and when. A viewer who sees the lead (its owner now, an
administrator organisation-wide) sees it all. The same rule applies wherever a deal is
shown, from this module: the pipeline's pages, lists and boards (`restrict`), its stage
history, agreed prices and corrections (`restrict_history`), global search (which matches
such a deal by what it shows them only, its organisation and instrument: `SHOWN_TEXT`, and
shows it through `restrict`), the activities' deal links (`title_for`), Ask Arkray's tools
(named the same way, citations included, its text withheld as here) and its retrieval
(which never returns such a deal's text: `visible_leads`); a support session sees exactly
what its user sees. A product decision recorded as conservative by default
(docs/adr/0032-privacy-remediation.md, risk R118); an administrator can always see the full
record.

Writes follow reads (deep security audit M-2): a viewer who may not see the customer can't
change it either. An edit that touches the customer copy (account, customer name, contact
phone and email, address) or a free-text custom value is refused (403 `customer_restricted`)
before anything is compared with what is stored, so a right guess and a wrong one get the
same answer, and nothing is written, versioned or audited. Overwriting it would also tamper
with what administrators, exports and Ask Arkray read, and slip past later corrections (which
only follow copies still holding the old value). The deal's other text that may describe
the customer (description, lost reason, work load, Expected CPT, instrument, custom values:
what a legal hold freezes and an erasure redacts) is read-only for such a viewer too (403
`customer_restricted` on a change), so no refusal reveals whether that customer was erased
or is held (review P3-2). Its value, probability and dates are a closed deal's sales record,
frozen for everyone (services.CLOSED_FROZEN, 422 `deal_closed`): such a deal is read-only to
its previous owner. Deleting a custom field's values from such a deal (removing the field)
is refused the same way (field_values.require_deletable).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from django.db.models import Case, CharField, Exists, F, OuterRef, Q, Value, When
from django.db.models.functions import Concat, Upper

from arkray.core.access import AccessScope
from arkray.core.errors import PermissionDeniedError
from arkray.leads.models import Lead

from . import naming
from .models import CustomField, FieldType, Opportunity

RESTRICTED_CUSTOMER = "Customer restricted"
CONTACT_FIELDS = ("customer_name", "contact_phone", "contact_email", "address")
FREE_TEXT_TYPES = frozenset({FieldType.TEXT, FieldType.LONG_TEXT})
# The deal's other free text, blank for such a viewer on every surface (L6, R118): what its
# salesperson (or an administrator) wrote about the deal, and so, readily, about the customer.
# `agreed_cpt` is the latest agreed price's, shown with the deal.
WITHHELD_TEXT = ("description", "lost_reason", "work_load", "expected_cpt", "agreed_cpt")
# The same in the deal's histories: a stage history row's lost reason, an agreed price's CPT,
# a correction's reason (services.correct_closed_deal).
WITHHELD_HISTORY_TEXT = ("lost_reason", "agreed_cpt", "reason")
# What a viewer who may not see the customer may not write either: the customer copy.
CUSTOMER_FIELDS = ("account_name", *CONTACT_FIELDS)
RESTRICTED_WRITE = (
    "This deal's customer now belongs to someone else, so its customer details can't be"
    " changed here."
)


# The deal's other text (description, lost reason, work load, Expected CPT, instrument, custom
# values): what a legal hold freezes and an erasure redacts. Read-only for such a viewer too, so
# a refusal never says whether the customer they no longer see was erased or is held (review
# P3-2: an edit answered 422 `lead_erased`).
RESTRICTED_TEXT = (
    "This deal's customer now belongs to someone else, so its details that may describe the"
    " customer can't be changed here."
)


class CustomerRestricted(PermissionDeniedError):
    """A write to the customer details of a deal whose customer the viewer may not see
    (403, nothing written; deep security audit M-2)."""

    code = "customer_restricted"
    default_message = RESTRICTED_WRITE


def visible(scope: AccessScope, lead_owner_id: UUID) -> bool:
    """Whether the viewer may see the deal's customer: they can see its lead."""
    return scope.permits_owner(lead_owner_id)


def visible_leads(scope: AccessScope) -> Q:
    """`visible` as a condition on opportunities (a join onto their lead), for queries that
    must leave such deals out altogether (Ask Arkray's retrieval)."""
    return Q() if scope.is_organization_wide else Q(lead__owner_id__in=scope.owner_ids)


def lead_visible(scope: AccessScope) -> Exists:
    """`visible` per opportunity row without a join (global search's restricted pass; an open
    deal's lead is always its owner's, so only closed ones ask)."""
    return Exists(Lead.objects.filter(pk=OuterRef("lead_id"), owner_id__in=scope.owner_ids))


# What such a viewer's search may match: the words `shown_title` shows them (the account
# unless it is the person's own name, and the instrument), upper-cased like every search
# text.
SHOWN_TEXT = Upper(
    Concat(
        Case(
            When(account_name__iexact=F("customer_name"), then=Value("")),
            default=F("account_name"),
            output_field=CharField(),
        ),
        Value(" "),
        "instrument_name",
    )
)


def refuse_hidden_writes(
    scope: AccessScope, lead_owner_id: UUID, pipeline_id: UUID, requested: Mapping[str, Any]
) -> None:
    """CustomerRestricted if the viewer may not see the deal's customer and `requested` (the
    edit's fields, before anything is compared with the stored values) names a customer field
    or a free-text custom field (of any state: a removed field's values are hidden too). The
    answer depends only on which fields were sent, never on their values."""
    if visible(scope, lead_owner_id):
        return
    touched = [field for field in CUSTOMER_FIELDS if field in requested]
    custom = requested.get("custom_fields")
    if isinstance(custom, Mapping) and custom:
        free_text = {
            str(pk)
            for pk in CustomField.objects.filter(
                pipeline_id=pipeline_id, field_type__in=FREE_TEXT_TYPES
            ).values_list("pk", flat=True)
        }
        if free_text & {str(key) for key in custom}:
            touched.append("custom_fields")
    if touched:
        raise CustomerRestricted(details={"fields": touched})


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
    for field in (*CONTACT_FIELDS, *WITHHELD_TEXT):
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


def restrict_history(rows: Sequence[dict[str, Any]]) -> Sequence[dict[str, Any]]:
    """Serialised rows of a deal's histories (stage history, agreed prices, corrections) as a
    viewer who may not see its customer gets them: amounts, stages, dates and who did what
    stay; their free text (WITHHELD_HISTORY_TEXT) is blank."""
    for row in rows:
        for field in WITHHELD_HISTORY_TEXT:
            if field in row:
                row[field] = ""
    return rows
