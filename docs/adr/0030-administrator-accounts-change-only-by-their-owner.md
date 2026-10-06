# 0030. An administrator's credentials and standing are changed only by that administrator

Status: Accepted (pending product confirmation of the off-boarding consequence below)
Date: 2026-10-06

## Context
The final audit found (risk R100) that one administrator could take over another: change the
other's sign-in email, then use *Forgot password* at the new address. Analysing the chain showed
a second route: demote the other administrator (allowed while another one remains), set a
temporary password for the now ordinary user (an administrator feature, ADR-0026), sign in as
them. Both end with the attacker holding the victim's account and the victim locked out. Every
step was audited, but nothing stopped it.

## Decision
For an actor who is not the target, and a target whose **role** is administrator (whatever its
status: active, invited or deactivated), these are refused (422) in the service layer, under the
user-administration lock, on the locked row, before the version check:
- changing the target's **email**;
- setting the target's **password**;
- changing the target's **role** (demotion).

Still allowed: name edits, deactivating and reactivating another administrator (off-boarding;
it exposes no credentials and keeps the last-administrator guard), resending an invitation (it
goes to the address on file), the administrator's own email change (with the current password),
and every management flow for ordinary users, including a temporary password and support
sessions. No exception for an invited administrator whose address was mistyped: deactivate and
invite the right address. The UI offers none of the refused actions. A password the user sets
is never visible to anyone; the temporary password an administrator types for a user is never
returned by any API.

## Consequences
- Takeover by email change, by demotion plus password set, or by any ordering of them is closed,
  including under concurrency (five race tests).
- An administrator can no longer be demoted through the UI (deactivate instead), and another
  administrator's mistyped email can't be corrected. Operators use deactivation or the
  documented shell procedure ([security.md](../security.md#administrator-account-protection)).
  A product decision may relax this (for example a verified workflow the attacking
  administrator cannot complete alone); until then the strict rule is the safe default.
- Residual: an administrator can still set an ordinary user's temporary password, sign in as
  them and, after the forced change, promote them. That gives no power an administrator lacks
  (they can invite an administrator at any address), and off-boarding reviews the accounts a
  departing administrator created or promoted.

## Alternatives considered
- **Only forbid the email change**: leaves demote-then-set-password.
- **A verified workflow** (the target confirms by email): the simplest version still lets an
  administrator who controls the old mailbox complete it; more is an IAM project the CRM does not
  need.
- **No cross-administrator deactivation**: would leave no way to remove a departed
  administrator without database access.
