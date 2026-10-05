# 0027. Leads removed from the UI; the lead stays as each deal's hidden customer record

Status: Accepted
Date: 2026-10-05

## Context
The user asked to "remove this whole lead section from the software", for salespeople and in
the administrator's view of a user's workspace, in line with the Bigin-style UI (Bigin has
pipelines and activities, no Leads list). But the lead is not a separate module: it is the
CRM's customer record (ADR-0015 deliberately has no Company or Contact entity). Every
opportunity and every activity has a NOT NULL, PROTECTed foreign key to a lead; composite
foreign keys bind an open opportunity's and current work's owner to the lead's owner
(ADR-0018, ADR-0020); the timeline, Ask Arkray's index and privacy erasure are keyed by lead;
and the only way to change a deal's owner was to reassign its lead. Removing the Leads
screen alone would have left the deal and activity forms asking for a lead nobody can create.

The user chose, of three options offered: hide Leads everywhere in the UI and let deals hold
the customer (over renaming Leads to Contacts, or deleting the lead tables and data).

## Decision
- **No Leads in the UI.** No Leads module, routes, header "New lead", dashboard lead figures
  or "New leads today" list, global-search Leads group, lead pages, Convert dialog or lead
  picker, in any workspace (own, organisation-wide, a user's opened by an administrator, a
  support session). Old `/leads…` URLs redirect to the same workspace's Pipeline.
- **The lead stays in the database as each opportunity's hidden customer record.** The schema,
  its constraints and lock order are unchanged; nothing is migrated or deleted, and existing
  leads keep their deals, activities and history.
- **A new opportunity brings its own customer record.** `POST …/opportunities` without `lead`
  creates, in the same transaction, a lead from the opportunity's customer details (customer
  name as the person, account name as the organisation, phone, email) through
  `leads.services.create_lead` (its audit, `LeadCreated`, its owner rules), then the
  opportunity. Its owner is decided as for a lead created in that workspace: the creator in
  their own workspace, the user in theirs, and organisation-wide an `owner` the request must
  name (`crm.assign_any`). Customer or account name is then required. The API still accepts
  `lead` (an existing customer record), with no `owner`.
- **Changing a deal's owner is "assign" on the deal.** `POST …/opportunities/{id}/assign`
  (`crm.assign_any`, open and unarchived deals, versioned) reassigns its customer record via
  `leads.services.reassign_lead`, so the customer's other open deals and current work move too,
  exactly as before; won and lost deals keep the owner who closed them. The UI says so.
- **Activities are about an opportunity.** The task and meeting forms pick a deal (recent open
  ones, or any found by global search); the customer is implied (the API already allowed an
  opportunity alone). The Activities list filters by opportunity instead of lead.
- **Search finds customers through their deals.** An opportunity's search text is its title,
  account name and customer name (a new trigram index, built before the old one is dropped);
  results carry the account and customer names. Matching the opportunity's own snapshot
  reveals nothing its viewer can't already see (the lead's name stays out: ADR-0022).
- **Wording.** Messages users can still meet say "customer" (for example "This customer's
  record is archived, so its activities can't be reopened."), never ask them to restore or
  reassign a lead, and Ask Arkray names a cited lead as a customer without linking it.

## Consequences
- The lead API (`/workspaces/{ws}/leads…`, conversion, lead timelines, lead options) and the
  dashboard's lead figures remain, documented and tested, but no screen uses them. Ask Arkray's
  lead tools stay (they answer questions about customers).
- Existing leads with several open deals: changing one deal's owner moves all of them.
- A customer record archived before this change (or by erasure) can't be restored from the UI:
  its deals can't be reopened or restored and take no new work. Administrators can restore a
  record through the API (`…/leads/{id}/restore`) if that is ever needed; erased ones stay so.
- Two deals for the same customer created from the UI get two customer records (there is no
  picker to reuse one); duplicates are harmless to every rule above.

## Alternatives considered
- **Rename Leads to Contacts** (smallest change, keeps one record per customer): declined by
  the user.
- **Delete the lead tables and re-key deals, activities, the timeline, Ask Arkray and erasure
  to the opportunity**: days of work across about a hundred test files, permanent data loss,
  and a high risk to the tested release candidate, for no behaviour the user asked for.
- **Hide only the Leads menu item**: deals and activities could then no longer be created.
