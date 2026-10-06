# 0029. Entering negotiation asks for the agreed price and the agreed CPT

Status: Accepted
Date: 2026-10-05

## Context
Since [ADR-0026](0026-user-pipelines-support-sessions-attachments.md) a deal entering a stage of
type *negotiation* must give its negotiated price, which is appended to an append-only history.
The business asked that moving a deal to negotiation ask for the **agreed price and the agreed
CPT**. CPT is still undefined in the CRM (no unit, type or meaning; see Expected CPT in
[ADR-0028](0028-opportunity-creates-its-lead.md)), so the same caution applies.

## Decision
- **Both are required, every time a deal enters negotiation**: a move, a reopen, a creation or
  a conversion directly into a negotiation stage (its type, whatever its name). A missing price
  and a missing CPT are reported together (400 `negotiated_price` / `agreed_cpt`); no other stage
  takes either (400). One rule in the services (`AgreedTerms`, `_terms_for`): drag and drop, the
  Move menu, the deal page, the New Opportunity panel, the API and an administrator's workspace
  all go through it.
- **The agreed price is the existing negotiated price.** The API field keeps its name
  (`negotiated_price`); the UI calls it *Agreed price* (the board card already said "Agreed").
- **The agreed CPT is one line of free text, ≤ 100 characters**, kept exactly like Expected
  CPT (whitespace collapsed, controls refused, a number sent as text). A blank CPT counts as
  not given, so it is asked for rather than recorded as an answer. No unit is invented.
- **Recorded with each price**: `pipeline_negotiation_price.agreed_cpt` beside `price` in the
  append-only history. Rows from before have `""`; nothing is guessed for them. The
  opportunity keeps **no copy**: the API's `agreed_cpt` (and Ask Arkray's) is the newest
  history row's, read in the detail, board and list queries (`selectors.LATEST_AGREED_CPT`,
  one probe of `pipeline_negotiation_opp_idx` per row; the board card shows it below the
  agreed price). A copy would go stale whenever the previous release (a
  rolling deploy, a rollback) records a price, leaving an older CPT beside a newer price, and
  the Update dialog would then offer it as agreed (review P2).
- **While negotiating, revisions take both** (`POST …/negotiated-prices {version, price,
  agreed_cpt}`): either may change. The same price *and* CPT as the latest row in this stage
  is a harmless retry; a different CPT at the same price is a new row. The deal page's
  *Update price & CPT* dialog starts from the CPT on record (it often stays while the price
  moves) and an empty price; until the user types a CPT it follows the deal, so a retry after
  a 409 sends the CPT someone else just recorded, never the one the dialog opened with.
- **The server's word reaches the field**: a refused price or CPT on a move is shown on that
  field in the confirmation dialog (the server refuses some pasted text the browser accepts,
  e.g. invisible characters); a New Opportunity refused for terms its stage didn't show (the
  stage became a negotiation stage meanwhile) reloads the pipelines so the fields appear.
- **Audit**: the stage change carries `negotiated_price_recorded` and `agreed_cpt_recorded`;
  revisions keep `opportunity.negotiated_price_recorded`. Values stay in the history, never in
  audit metadata. The idempotency digest of a create or conversion includes the CPT only when
  one is sent, so a request without one keeps the previous release's digest and its retry
  across a deploy still replays.
- **Ask Arkray**: `get_record` and `get_negotiation_history` return the agreed CPT with each
  price (`latest_agreed_cpt`). It is not searchable and not in the semantic index (like
  Expected CPT).
- **Migration `pipeline.0009`**: the column is `varchar(100) NOT NULL DEFAULT ''` and keeps
  its database default (`db_default`), so the previous release, which never names it, can
  still insert during a rolling deploy or after a rollback (tested with its INSERT shape).
  Catalogue-only in PostgreSQL 11+ (no rewrite, no backfill); reversible.

## Consequences
- An API client entering negotiation with only a price now gets 400 (`agreed_cpt`). Only the
  UI and the test-suite use the API.
- During a rolling deploy the previous release's UI can't move a deal into negotiation on the
  new backend (it sends no CPT): the move is refused with the field named, nothing is changed.
- Deals already negotiated keep their price without a CPT until the next revision or re-entry,
  which asks for one.
- What CPT means and its unit remain an open business question (docs/pipeline.md#expected-cpt);
  a typed field can follow once confirmed, for both Expected and Agreed CPT.

## Alternatives considered
- **An optional CPT**: rejected; the request is that negotiation *asks* for it, and an optional
  field beside a required price is easily skipped.
- **Storing the CPT only on the opportunity**: rejected; a revision could then change the CPT
  without a trace, while every price is kept in an append-only history.
- **A copy of the latest CPT on the opportunity** (like `negotiated_price`): built first, then
  removed after review: the previous release updates the price copy but not a CPT copy.
- **A numeric or currency CPT**: would invent a unit nobody has confirmed (as for Expected CPT).
- **Renaming `negotiated_price` to `agreed_price` in the API and database**: a breaking rename
  of a column, an API field, audit keys and Ask Arkray output for a wording change; the label is
  changed in the UI only.

## Addendum (final audit remediation, 2026-10-06): CPT stays text until the product owner decides

**CPT business meaning/unit requires product-owner confirmation** (risk R104, open). Nothing
in this ADR assumed one, and the remediation keeps it that way; it only made the text rule
explicit and single:

- One rule for both CPTs: `pipeline.validation.clean_cpt` (with `CPT_MAX_LENGTH` = 100, also
  the bound of both columns), used by every write path. One line of text, kept as written;
  invisible and control characters refused (core.text); at most 100 code points.
- **Changed**: a JSON number, boolean, list or object is now a 400 for `expected_cpt` and
  `agreed_cpt` (it used to be turned into text, so `18.50` was stored as `"18.5"`: the
  salesperson's figure silently re-spelled). The UI always sent strings.
- Never interpreted: not in totals, search, the semantic index or audit metadata; Ask Arkray
  repeats it as written. No CSV export exists.
- A typed CPT later is an *expand / backfill / contract* change beside the text columns
  (docs/pipeline.md#expected-cpt); `expected_cpt` and `agreed_cpt` keep their meaning for API
  clients. No schema change was made now.
