# 0034. Security remediation before staging: hold-safe deletion, closed-deal corrections, fair queues, production-only credentials, sign-in limits, final pseudonymisation

Status: Accepted (engineering); the product owner's decisions it names stay open ([production-checklist.md](../production-checklist.md#owner-decisions-pending))
Date: 2026-10-12

## Context
An adversarial review of `73ff1e3` (seven reviewers: sign-in and sessions, access to records,
privilege escalation, uploads and injection, AI and search, business logic, infrastructure and
the frontend) found no Critical or High issue, 6 Medium and 14 Low findings, and a list of
hardening suggestions. The ones that needed a decision rather than a local fix:

- **M1**: deleting an Ask Arkray conversation answered 409 `legal_hold` when the caller's own
  question named any held customer in the organisation, else 204. The hold rule reads every
  hold, unscoped, so the status code told a sales user whether a colleague's customer exists
  and is under a legal or data-request hold, and a held user that they were held.
- **M2**: a won deal's value, probability and dates stayed editable by its owner, and the audit
  recorded the field name only, so closed revenue could be inflated without a trace of by how
  much.
- **M3, M6**: one account could grow the erasure ledger without limit (non-idempotent purges,
  one entry per file, a head found by listing the whole ledger) and starve the shared queues
  (first-come claiming, no write limit beyond 600 requests a minute, unbounded per-customer
  data behind exports and erasures).
- **M4**: the production-shaped stack read its secrets under the names the development files
  use, so the checkout's `.env` (the published development superuser password) filled them
  whenever the operator's file wasn't exported; the superuser was reachable from every
  container and no password's strength was checked.
- **L1, L3**: password guessing was slow but never stopped (about 552 guesses a day per
  account, the owner's own sign-in forgave the attacker, timing told real emails apart), and a
  compromised administrator could take over an ordinary account and promote it out of every
  other administrator's reach (R100's rule), which ADR-0033 had accepted.
- **L4**: "pseudonymised" was an email-domain test that user administration never asked, so an
  administrator could rename or reactivate a former employee's account and make it a live login
  again.

## Decision

### Deleting an Ask Arkray conversation always succeeds; a hold keeps a tombstone (R148)
- `DELETE …/ask/conversations/{id}` always answers 204 with the same headers and the same
  statements, whether or not a hold keeps the conversation (`ai.subjects.hold_keeps` evaluates
  the user-hold half and the held-customer half every time).
- A conversation no hold keeps is deleted as before. One a hold keeps becomes a **tombstone**
  (`ai_conversation.forgotten_at`, `ai.0004`): every read excludes it (the list, the detail,
  question polling, continuing it, a second deletion: 404), its pending questions are failed
  `forgotten` (no bulkhead count, never answered), and the hourly housekeeping deletes it once
  no hold keeps it, whatever its age. It remains the hold's evidence: exports and erasures
  still match it.
- The hold barrier's serialisation (R134) is unchanged. The audit records `retained`, never
  which hold.

### Closed deals are frozen; administrators correct them with an append-only history (R149)
- A won or lost deal's value, probability, expected close date and opportunity date can't be
  changed by the normal edit, by anyone: 422 `deal_closed` (`services.CLOSED_FROZEN`, checked
  before anything is compared or written). Reopening makes them editable again.
- An administrator corrects them with `POST …/opportunities/{id}/correct` (`crm.manage_any`, a
  recent second factor, never in a support session, a reason, the version under the row lock),
  which appends the figures before and after, the reason, who and when to
  `pipeline_opportunity_correction` (`pipeline.0011`), append-only by trigger like the stage
  history; the audit names the fields and the row only. Allowed under a legal hold (it
  overwrites none of the held text), refused for an erased customer (its reason would be new
  free text about them). The probability isn't correctable (the database fixes it at 100 or 0).
- The same review (L6) settles R118's two surfaces on **one rule** for a closed deal's previous
  owner, defined once in `pipeline/customer.py`: no customer identity or contact details and
  none of the deal's free text, on every API surface and in Ask Arkray's tools.

### Fair claiming and write budgets; an idempotent, constant-cost ledger (R150, R153)
- Every outbox event carries a **fairness key** (the acting user, inherited by follow-up work;
  `''` for system work). The relay takes turns between keys (a key's k-th waiting event with n in
  flight goes in turn n + k), stays work-conserving and first-in, first-out within a key
  (`core.0007`, `core.0008`). A per-key in-flight cap was rejected: it would cap one
  administrator's legitimate bulk re-index.
- **Write budgets** on creation (`ACTIVITY_MAX_CREATED_PER_USER_PER_DAY`, 1,000 in any 24
  hours, counted from the activities themselves through an index of their own, at constant
  cost; `ACTIVITY_MAX_PER_CUSTOMER`, 5,000), and on asking for custom values' deletion
  (`MAX_VALUE_DELETIONS_PER_USER_PER_DAY`, 100 fields a day, each written to the ledger for
  good). Large jobs are split (`ai.index_lead` in batches of 200, an erasure's file purge as
  one paged job), the customer export streams and its cap counts the compressed archive, the
  erasure runs in bounded statements.
- The erasure ledger grows only with data really removed: value purges are idempotent, one
  request is one job and one entry, files purged together share one entry, and finding the head
  costs one small LIST and one GET from the last verified entry. The gate reads the database's
  position first and closes at once on a database behind the verified chain; ledger reads wait
  at most 2 s and decide "ahead" on their own, so a slow store can't keep a restored database's
  gate open, and a verdict that the chain was tampered with holds, logged as a paging
  security event at every health probe, until the whole chain verifies again from scratch.
  **Signed checkpoints** (one per 1,000 entries, in the ledger's own store beside the entries, an HMAC
  with the ledger key) let a process start without re-reading the whole chain; every worker
  container still verifies it from scratch at least hourly, in slices. The entry format is
  unchanged, so a previous release still verifies the chain (its replay of a batched entry
  fails closed).

### Production-only credential names, a credential guard and `pg_hba.conf` (R151)
- The production-shaped stack reads every secret under a production-only name
  (`ARKRAY_PROD_*`) from a file given with `docker compose --env-file`, which Compose then reads
  instead of `.env`; no development file defines one. Requiring the names was chosen over
  documenting `set -a`: a missing name now fails, where a sourcing mistake failed open.
- Every Compose command runs through `scripts/prod-compose.sh`, which drops the shell's
  `ARKRAY_PROD_*` first: an exported variable beats `--env-file`.
- A one-shot `credential-guard` (`config/credentials.py`, no network) that every service waits
  for refuses missing, published, placeholder, weak and reused values, naming variables only.
  Weak is judged for generated values only: under 32 characters, too few different ones, not
  evenly random, or any recognisable pattern (repeats, runs, joined words, common password
  words), so a human passphrase is refused on purpose. Production settings refuse a published
  development password; `manage.py preflight --only credentials` applies the whole rule.
- `infrastructure/postgres/pg_hba.conf`: the superuser on the local socket only (shared with
  the network-less role-setup job, to keep the one-command start), physical replication only
  for it on that socket, the owner only from the migrate job's network, the application role
  over TLS, both to `arkray` and one fixed restore database, `arkray_restored`, renamed into
  place after a restore (a rename keeps the overlay unchanged), SCRAM, everything else rejected.
- Related (L14, R169): `ARKRAY_RUNTIME=production` in the backend image makes an unset
  `DJANGO_SETTINGS_MODULE` mean production's and refuses any other module, and the development
  and test settings refuse to load under it; gunicorn defaults to production settings outside
  the image too.

### Sign-in limits per network and per day, equal work, two-step verification for everyone (R154, R156)
- An unrecognised browser's budget is per account **and network** (IPv4 /24, IPv6 /64); a
  success forgives that browser's failures without deleting them.
- A **daily cap**: 50 failures from unrecognised browsers in 24 hours lock the account's
  sign-in from them (`identity_login_lock`, 429 `sign_in_locked`, identical for an unknown
  email) until the owner's emailed unlock link (which names the account before it is used and
  trusts the browser that opens it, never in place of another account's trust there, so a new
  storm can't lock that browser out; any credential change revokes a pending link), a reset,
  an administrator or 30 days. Trusted
  browsers keep working. A cap with an emailed unlock was chosen over a longer automatic
  lockout: whoever knows an email can trip any cap, and only the owner's mailbox tells the
  owner apart.
- **Equal work**: an unknown email's failed sign-in writes the same audit rows in a savepoint
  and rolls them back, so both paths run the same statements and nothing an anonymous caller
  chose remains (R135's acceptance superseded).
- **`MFA_REQUIRED_FOR_ALL_USERS`** (production default `true`, `false` accepted as an
  organisational decision) extends ADR-0033's requirement from administrators to everyone.
- **Promote-then-shield** (amends ADR-0033's "Decisions taken consciously"): promotion to
  administrator is refused for 7 days (`ADMIN_PROMOTION_COOLING_OFF_S`) after an administrator
  set the account's password, changed its email, reset its second factor or created it with a
  password, read from the append-only audit trail; the owner is emailed about an
  administrator-set password and every role change, at the address the account had when the
  notice was queued, whatever its status by then; promotions alert.

### Pseudonymisation is final (R157)
- `User.pseudonymised_at` marks the account; a CHECK holds a marked account to its pseudonym
  and a trigger keeps the mark (the application role can't clear it); every user-administration
  change is refused with 422 `account_pseudonymised`. `identity.0008` marks earlier ones,
  corroborated by their audit event, and puts revived accounts back under their pseudonym; a
  live account merely given a pseudonym-domain address is reported, never pseudonymised, and
  the domain is reserved. A returning person is invited as a new user.

### Independent review (rounds 1 to 4)
Independent reviewers checked these fixes in rounds, each round's findings fixed before the
next began (red first, or by a mutation restoring the earlier behaviour). The decisions above
describe the state after them; R148-R170 record each change and what it left open.

- **Round 1** found 6 Medium, 23 Low and 14 informational issues, all fixed except INFRA-3,
  mitigated with a documented requirement (R165), and one pre-existing note (AI-4): among them
  a quadratic-time regression in the AI masking (AI-1), a CSV formula check costing seconds
  per upload (PARSERS-1), unbounded archived pipelines (DEALS-1), the hardened `pg_hba.conf`
  refusing the documented restore (INFRA-1), a ledger gate a slow store kept open (LEDGER-1)
  and per-field ledger entries with O(n) starts (LEDGER-2). One pre-existing issue it
  surfaced, two locks sharing an advisory-lock namespace, is now refused by a test (R170).
- **Round 2** verified every round-1 fix of the deals, identity, infrastructure and queues
  (all closed, no new Medium) and found 3 Medium in the upload parsers (crafted CSVs,
  workbooks and documents still costing seconds of CPU, and an embedded package's central
  directory: hence one work pool per upload and end records read before `zipfile` parses a
  directory), 2 Medium in the erasure ledger (a tampered verdict that flapped with
  checkpoints, never alerting, and recycled web workers closed for a time growing with the
  ledger: hence a sticky, logged verdict, checkpoints outside the entries' listing and a
  refresh before each fork) and Lows in the AI masking; all fixed.
- **Round 3** found 1 latent Medium in the AI masking (round 2's reference-word suppression
  let a word ending one sentence hide the next sentence's phone or Aadhaar number: now only a
  label right before a figure keeps it) and three Lows; 2 Medium in the upload parsers (a
  quadratic whole-formula check, 17-25 s of CPU from a 2-7 KB upload, and content-type and
  relationship parts parsed whole and repeatedly, 5.9 s and 130 MiB from 2 KB: hence a linear
  judge, streamed small parts read once, duplicate part names refused, every member priced, and
  a structural per-upload CPU deadline of 5 s); and Lows only in the erasure ledger (a slow
  store reopening a rolled-back ledger, the pre-fork refresh not bounded as a whole, the purge
  backoff falling back to blocking without the cache: hence decided verdicts that stand,
  appends that refuse a foreign chain, one deadline for the refresh, the backoff on the row).
  All fixed.
- **Round 4** found 1 Medium (borderline Low) and 1 Low in the upload parsers (a quadratic
  Tifinagh joiner check, a round-3 regression, and unpriced central-directory names and extra
  fields: now checked a character at a time, and the records walked and priced before
  `zipfile` parses them), and Lows only in the erasure ledger (an old entry replaced by
  something that isn't an entry passing as a fault, and the refresh writing a checkpoint past
  its deadline). All fixed; nothing remains open from the reviews but the residuals and owner
  decisions recorded in R148-R170.

## Consequences
- No answer of the API tells a user whether someone else's customer is under a hold; a held
  user's own deletion completes after the release, within the hour.
- Closed revenue can't be changed silently: every change after the close is either a reopen
  (in the stage history) or an administrator's correction (old and new figures, reason, who).
  Sales users who fixed a typo on a won deal now ask an administrator, or reopen and close
  again.
- One account can no longer delay everyone's background work beyond a turn per user, nor make
  a customer's data-rights requests fail; a heavy legitimate user meets the budgets at 1,000
  activities a day.
- The production-shaped stack can't start on a development or weak secret; every operator
  command names the secrets file. Role setup, migrations, backups (over the socket) and the
  documented restore keep working.
- Where many people share one public address, the edge alone can't keep one of them from
  holding that address's sign-ins and saves (nginx has no whole-request deadline): a front
  proxy with a total request deadline is a production requirement for that topology (R165).
- Every sales user enrols a second factor at their next sign-in after the upgrade (unless the
  organisation sets `MFA_REQUIRED_FOR_ALL_USERS=false`); someone who knows an email can still
  lock that account's *new* browsers until the owner uses the emailed link.
- A former employee's account can't come back. Every column this release adds is nullable or
  has a database default, so the previous release can still insert, but its pods don't apply
  these rules while they run, so a stopped deploy is preferred when holds are active
  ([deployment.md](../deployment.md#which-previous-release-can-run-beside-the-new-schema)).
- Residuals and owner decisions are listed per finding in the risk register (R148-R170) and in
  [production-checklist.md](../production-checklist.md#owner-decisions-pending).

## Alternatives considered
- **M1: scope the hold match to the caller's own customers** (keep 409 for one's own held
  customers): still a refusal a held user learns from, and the bulkhead and timing side
  channels remain; the tombstone answers everyone alike.
- **M2: forbid any change after the close, without a correction path**: a genuine mistake would
  then need a reopen and a second close, distorting the stage history; a dedicated capability
  or two-person approval for corrections was left to the product owner.
- **M2: keep only an audit of old and new values**: audit metadata never holds amounts by
  design (ADR-0032); an append-only table keeps them without loosening that rule.
- **M6: a per-user in-flight cap in the outbox**: rejected (it re-creates the Phase 10
  regression that throttled an administrator's bulk re-index to 4 events a second).
- **M3: every process verifying the whole chain at its start** (the round-0 design): O(n) at
  each start, 33 s at 10,000 entries on a slow store, past a health check's 30 s, so a large
  ledger left restarted workers unhealthy for good (review round 1). Signed checkpoints were
  chosen instead; the hourly pass from scratch in every worker container keeps an old entry
  altered after its checkpoint from going unnoticed.
- **M3: a per-user budget on every ledger-producing operation**: only the value deletions got
  one (they could write an entry per field at the API's rate); file purges are batched and
  bounded by the upload throttle and quota.
- **M4: only fix the documentation (`set -a`)**: the mistake stays possible and fails open.
- **L1: a breached-password API**: no external call from the sign-in path; Django's 20,000
  common passwords are refused on every path, `createsuperuser` included, and two-step
  verification closes most of what a breached-password check would.
- **L3: forbid promotion of any account an administrator ever touched**: too broad (every
  invited user's lifecycle involves an administrator); 7 days after a credential change, with
  the owner told, bounds the takeover window.
