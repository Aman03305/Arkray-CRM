"""Correcting a customer's details (docs/adr/0032-privacy-remediation.md, privacy remediation
P2-6; docs/privacy.md#correction).

Since ADR-0028 a lead is the canonical customer record behind its opportunities, and its
page is read-only. A person who asks for their details to be corrected (a wrong email, a
misspelt name, an old address) is served here, narrowly:

- **What can change**: the lead's identity and contact fields only (`CORRECTABLE`: name,
  organisation, job title, email, three phone numbers, address). Not its status, owner,
  source, description or dates; not any opportunity's commercial details.
- **Copies follow, history doesn't**: every opportunity of the lead (open, closed or
  archived) whose customer copy still holds the lead's *previous* value of a field gets the
  corrected one (customer name, account, contact phone and email, address), and its derived
  name follows (naming.py). A copy someone deliberately made different (the lab buying
  isn't the lead's organisation) is left alone, as are the deal's value, stages, prices,
  CPTs, notes and the append-only history: corrections fix who the customer is, never what
  was agreed.
- **Who**: whoever may edit in the workspace that sees the lead (its owner in their own
  workspace, an administrator); a closed deal's previous owner can't see the lead and can't
  correct it. An erased lead can't be corrected.
- **Safely**: one transaction, the lead locked first and then its opportunities in id
  order (docs/pipeline.md#lock-order); the lead's `version` must match (409 otherwise), and
  every changed record gets a new version, so a stale edit of a corrected deal is refused
  too. Audited by field names only (`lead.corrected`, `opportunity.customer_corrected`), and
  the domain events re-index Ask Arkray from the corrected text; search and the dashboard read
  the live columns.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from arkray.audit import services as audit
from arkray.core import holds
from arkray.core.access import AccessScope
from arkray.core.domain_events import publish
from arkray.core.errors import BusinessRuleViolation, ConflictError, InvalidInputError
from arkray.core.models import HoldSubject
from arkray.identity.models import User
from arkray.identity.workspaces import authorize_write
from arkray.leads import events as lead_events
from arkray.leads import selectors as lead_selectors
from arkray.leads import validation as lead_validation
from arkray.leads.models import Lead
from arkray.leads.phones import phone_key

from . import events, naming
from .models import Opportunity

AUDIT_LEAD_CORRECTED = "lead.corrected"
AUDIT_OPPORTUNITY_CORRECTED = "opportunity.customer_corrected"
ERASED = "[erased]"
ERASED_REFUSED = "This customer's details were erased on request: there is nothing to correct."
CORRECTABLE = (
    "first_name",
    "last_name",
    "organization_name",
    "job_title",
    "email",
    "phone",
    "mobile",
    "alternate_phone",
    "address_line_1",
    "address_line_2",
    "city",
    "state",
    "postal_code",
    "country",
)
PHONES = ("phone", "mobile", "alternate_phone")


@dataclass(frozen=True, slots=True)
class Correction:
    lead: Lead
    fields: tuple[str, ...]
    opportunities: int  # how many deals' customer copies followed


def _name(first: str, last: str) -> str:
    return f"{first} {last}".strip()


def _address(values: Mapping[str, Any]) -> str:
    """The lead's address as an opportunity's copy holds it (services._address_lines)."""
    return "\n".join(line for line in (values["address_line_1"], values["address_line_2"]) if line)


def _same_text(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def _copy_changes(
    opportunity: Opportunity, before: dict[str, Any], after: dict[str, Any]
) -> dict[str, str]:
    """The opportunity's customer copy, corrected where it still holds the old value."""
    changes: dict[str, str] = {}
    old_name, new_name = (
        _name(before["first_name"], before["last_name"]),
        _name(after["first_name"], after["last_name"]),
    )
    if old_name != new_name and old_name and _same_text(opportunity.customer_name, old_name):
        changes["customer_name"] = new_name or after["organization_name"]
    old_org, new_org = before["organization_name"], after["organization_name"]
    if old_org != new_org and old_org and _same_text(opportunity.account_name, old_org):
        changes["account_name"] = new_org or new_name
    elif (
        not old_org
        and old_name != new_name
        and old_name
        and _same_text(opportunity.account_name, old_name)
    ):
        # A person-only lead's account is its name (leads.selectors.customer_names).
        changes["account_name"] = new_org or new_name
    if (
        before["email"] != after["email"]
        and before["email"]
        and _same_text(opportunity.contact_email, before["email"])
    ):
        changes["contact_email"] = after["email"]
    copied = phone_key(opportunity.contact_phone)
    for field in PHONES:
        if before[field] != after[field] and copied and copied == phone_key(before[field]):
            changes["contact_phone"] = after[field]
            break
    old_address, new_address = _address(before), _address(after)
    if old_address != new_address and old_address and opportunity.address.strip() == old_address:
        changes["address"] = new_address
    return {
        field: value for field, value in changes.items() if getattr(opportunity, field) != value
    }


def correct_customer(
    *, actor: User, scope: AccessScope, lead_id: UUID, version: int, changes: Mapping[str, Any]
) -> Correction:
    authorize_write(actor, scope)
    unknown = set(changes) - set(CORRECTABLE)
    if unknown:
        listed = ", ".join(sorted(unknown))
        raise InvalidInputError(
            details={"non_field_errors": [f"Only customer details can be corrected: {listed}."]}
        )
    cleaned = lead_validation.clean_fields(changes)
    with transaction.atomic():
        lead_selectors.lock_lead(scope, lead_id, exclusive=True)
        lead = Lead.objects.get(pk=lead_id)
        if lead.first_name == ERASED:
            raise BusinessRuleViolation(ERASED_REFUSED)
        if holds.is_held(HoldSubject.LEAD, lead.pk):
            # The held record is evidence: corrected only once the hold is released (the
            # lead's lock is held, so a hold placed now waits for this to finish).
            raise holds.UnderLegalHold("lead", lead.pk)
        if lead.version != version:
            raise ConflictError()
        before = {field: getattr(lead, field) for field in CORRECTABLE}
        changed = [field for field, value in cleaned.items() if getattr(lead, field) != value]
        if not changed:
            return Correction(lead_selectors.lead_by_id(lead.pk), (), 0)
        for field in changed:
            setattr(lead, field, cleaned[field])
        lead_validation.require_identity(lead.first_name, lead.last_name, lead.organization_name)
        after = {field: getattr(lead, field) for field in CORRECTABLE}
        lead.version += 1
        lead.save(update_fields=[*changed, "version", "updated_at"])  # phone_keys follow
        fields = tuple(sorted(changed))
        audit.record(
            AUDIT_LEAD_CORRECTED,
            actor_id=actor.pk,
            target_type="lead",
            target_id=lead.pk,
            subject_user_id=lead.owner_id if lead.owner_id != actor.pk else None,
            metadata={"workspace": scope.kind.value, "fields": list(fields)},
        )
        publish(
            lead_events.LeadUpdated(
                lead_id=lead.pk,
                owner_id=lead.owner_id,
                actor_id=actor.pk,
                occurred_at=lead.updated_at,
                fields=fields,
            )
        )
        followed = _follow(actor, lead, before, after)
    return Correction(lead_selectors.lead_by_id(lead.pk), fields, followed)


def _follow(actor: User, lead: Lead, before: dict[str, Any], after: dict[str, Any]) -> int:
    now = timezone.now()
    entries: list[audit.Entry] = []
    copies = (
        Opportunity.objects.select_for_update(no_key=True, of=("self",))
        .filter(lead_id=lead.pk)
        .order_by("pk")
    )
    for opportunity in copies:
        changes = _copy_changes(opportunity, before, after)
        if not changes:
            continue
        for field, value in changes.items():
            setattr(opportunity, field, value)
        written = list(changes)
        if naming.SOURCES & set(changes):
            title = naming.opportunity_title(
                customer_name=opportunity.customer_name,
                account_name=opportunity.account_name,
                instrument_name=opportunity.instrument_name,
            )
            if title != opportunity.title:
                opportunity.title = title
                written.append("title")
        opportunity.version += 1
        opportunity.updated_at = now
        opportunity.save(update_fields=[*written, "version", "updated_at"])
        reported = tuple(sorted(written))
        entries.append(
            audit.Entry(
                AUDIT_OPPORTUNITY_CORRECTED,
                actor.pk,
                "opportunity",
                opportunity.pk,
                opportunity.owner_id if opportunity.owner_id != actor.pk else None,
                {"fields": list(reported), "lead_id": str(lead.pk)},
            )
        )
        publish(
            events.OpportunityUpdated(
                opportunity_id=opportunity.pk,
                lead_id=lead.pk,
                owner_id=opportunity.owner_id,
                actor_id=actor.pk,
                occurred_at=now,
                fields=reported,
            )
        )
    audit.record_many(entries)
    return len(entries)
