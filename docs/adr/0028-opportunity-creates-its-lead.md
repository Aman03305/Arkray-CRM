# 0028. A new opportunity creates its lead; opportunities are named after their customer and instrument

Status: Accepted; amended by 0032 (a narrow correction of the customer's details, synchronised to deal copies, replaces "read-only lead page")
Date: 2026-10-05

## Context
After [ADR-0027](0027-leads-removed-from-the-ui.md) the New Opportunity form still asked for an
"Opportunity name", and users still reached for an existing lead first. The business asked for a
direct, simple workflow: enter the customer and deal, press Create, see it in the pipeline, with
the lead existing automatically. It also asked for:
- the **lead to stay the canonical customer/prospect record**: every new opportunity is one new
  lead for counting, the Dashboard's Total Leads and New Leads Today come from the Lead table,
  and today's new leads are listed (before Upcoming Meetings) and open a lead page that leads
  to the opportunity;
- **Instrument** as a choice of exactly "Adams 8380 V-lite", "Adams 8180 V", "Adams 8180 T" and
  "PCBA with Printer", by drag and drop but also by click, tap and keyboard, validated by the
  server, defined once;
- an **Expected CPT** field. Nothing in the CRM's documentation or data defines CPT (no unit,
  type or meaning), so the request said to keep it conservative and flag the semantics;
- Ask Arkray to answer "new leads today" with the Dashboard's figure, and "opportunities for
  <instrument>" from structured data, without counting a lead and its opportunity twice.

## Decision
- **One creation, two records, one transaction** (unchanged mechanism, ADR-0027):
  `POST …/opportunities` without `lead` creates the lead through `leads.services.create_lead`
  (its audit, `LeadCreated`, owner rules) and then the opportunity, linked to it, in one
  `transaction.atomic()`; any failure rolls both back. The existing Idempotency-Key handling
  makes a retry or a double submit (also concurrent) replay the first creation: one lead, one
  opportunity. `opportunity.created` now records `lead_created: true`. The lead is then
  **Converted** ([ADR-0019](0019-lead-conversion.md): it has an opportunity), by the leads
  module's own status change, exactly as converting a lead leaves it.
- **Field mapping** (deliberate, nothing duplicated into unrelated fields): customer name → the
  lead's name; account name → its organisation; phone and email → its phone and email; the
  address → its two address lines *only when it fits them* (≤ 2 lines of ≤ 200 characters),
  otherwise it stays on the opportunity only (never cut, never guessed into city/state/postal
  code); owner → the opportunity's owner; created time → the database's. Instrument, work load,
  prices, Expected CPT and dates are the deal's and stay on the opportunity. The lead is a
  snapshot made at creation: later edits of the opportunity's customer details don't rewrite
  it (a lead may have several opportunities).
- **Nobody names an opportunity.** `title` is gone from every write API (create, update,
  convert) and from the services' accepted fields; the services set it
  (`pipeline/naming.py`): `"<customer name> — <instrument>"`, the account name when there is no
  customer name, just the customer when there is no instrument, shortened with "…" to the 200
  characters it is stored in. It is stored (the board, search, Ask Arkray, history and
  notifications keep reading `title`) and re-derived whenever the customer name, account name
  or instrument changes. An opportunity named before this keeps its typed name until one of
  those changes. No generated numbers.
- **Instruments are one Python tuple**, `pipeline/instruments.py`, served by
  `GET /api/v1/config/opportunity-options`. The services accept a listed instrument (case and
  spacing aside; stored in the list's spelling) or none, for a new opportunity and for a
  *changed* instrument; an older opportunity's free text is kept while unchanged. No database
  constraint (older rows). Adding one is one line and no migration; renaming one would need a
  data migration, so names stay stable. The UI fetches the list (nothing hard-coded) and offers
  a radio group with a drop zone: click/tap, Tab + arrow keys + Space/Enter, or drag.
- **Expected CPT** (`expected_cpt`): optional free text, one line, ≤ 100 characters, like Work
  load (its column keeps a database default, so the previous release can still insert during
  a rolling deploy or after a rollback): no unit, type or meaning is invented. In the API, the detail page, the edit panel, the
  audit (field name only) and Ask Arkray's record details; not in search or the semantic index.
  **The business must confirm what CPT means and its unit**; a typed field can follow then.
- **The lead is visible again, read-only.** A lead page at `/leads/{id}` (and under a user's
  workspace) shows the customer, contact details, owner, creation time and its opportunities
  (each a link); the opportunity page links back ("Lead"). The Dashboard shows Total leads and
  New leads today again, and a compact **New leads** panel *before* Upcoming meetings (name,
  instrument, owner organisation-wide, time; the name opens the lead page). Global search shows
  a **Leads** group again, labelled, beside Opportunities. Ask Arkray links cited leads. There
  is still **no Leads module**: no Leads list, form, edit page or navigation item; those old
  URLs still redirect to the Pipeline.
- **Duplicates are not merged.** Names are not unique, so a new opportunity always gets a new
  lead. The existing scoped duplicate check (same phone or email, in the workspace only) shows
  a non-blocking "Possible existing lead" notice in the form; nothing is linked silently.
- **Archive and restore are unchanged**: archiving an opportunity never archives or deletes its
  lead (the lead still counts as a lead; the dashboard row then shows no opportunity); an
  archived lead's opportunities can't be restored or reopened (as before); nothing cascades and
  no history is deleted.
- **Ask Arkray**: lead counts come only from `get_lead_summary` (the Dashboard's selector);
  the system prompt and tool description say an opportunity and its lead are one customer,
  never two leads. The router answers "How many (new) leads were created today?" and
  "Show (open) opportunities for <instrument>" deterministically; `list_opportunities` takes an
  `instrument` (the field, from the list) and returns customer and instrument.

## Consequences
- Clients can no longer send `title` (400 "Unknown field(s): title."). Only the UI and the
  test-suite use the API.
- Two opportunities for the same customer made from the form are two leads (by design, as in
  ADR-0027); the duplicate notice makes it visible.
- The dashboard runs one more bounded query (the new leads' opportunities, at most five rows
  through an index; none when there are no new leads).
- Expected CPT's meaning is an open business question (recorded in docs/pipeline.md).

## Alternatives considered
- **A separate "pipeline lead" model or merging leads by customer name**: rejected by the
  request; names aren't identities.
- **An instrument table managed by administrators**: more moving parts (an admin screen,
  migrations of seed rows) for four fixed values; the tuple can become a table later without
  changing the API (`/config/opportunity-options` already returns objects).
- **A numeric/currency Expected CPT**: would invent a unit nobody has confirmed.
- **Computing the opportunity's name at read time**: every reader (board, search index, Ask
  Arkray, history, notifications) would need the rule; storing it keeps one writer.
- **Restoring the whole Leads module**: the user removed it in ADR-0027; a read-only page gives
  the requested navigation without a second place to edit a customer.
