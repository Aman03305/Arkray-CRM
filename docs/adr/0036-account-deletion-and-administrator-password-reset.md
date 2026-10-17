# 0036. Deleting accounts and resetting passwords from user administration

Status: Accepted (owner's requirements of 2026-10-16); amends
[0030](0030-administrator-accounts-change-only-by-their-owner.md) (a super administrator may
erase a team administrator's account) and builds on
[0035](0035-super-administrators-and-teams.md)
Date: 2026-10-16

## Context
ADR-0035 gave the organisation a Super admin, Admins (team administrators) and Users. The
owner now asks for two more account operations in user administration, enforced by the server:

| Action | Super admin | Admin | User |
|---|---|---|---|
| Create Admin | yes | no | no |
| Create User | yes | yes (into their team) | no |
| View Admin accounts | yes | no | no |
| View managed users | yes | yes (their team) | no |
| **Delete** Admin | yes | no | no |
| **Delete** User | yes | yes, own team only | no |
| **Reset** Admin password | yes | no | no |
| **Reset** User password | yes | yes, own team only | no |
| Change own password | yes | yes | yes |

Nobody deletes or resets a **Super admin** here (deny by default: the matrix grants it to no
one; a Super admin asking gets 403, an Admin, who never reaches the account, 404), nor their
own account (422).

Until now users were never deleted (their id attributes the CRM history they made),
administrators could only deactivate an account and a Super admin could pseudonymise a
deactivated one in a separate step (privacy.staff), never an administrator's. An administrator
could set a user's *temporary password* (ADR-0026) but nobody could send an Admin a reset link.

## Decision

### Reset password: an emailed single-use link, never a password
`POST /api/v1/admin/users/{id}/reset-password` (`users.manage` or `users.manage_team`, reach
applied: 404 outside it, identical to a missing account; recent two-step verification where
the actor's role requires it (Super admins, ADR-0033); throttled; not in a support session).

- Allowed targets: an **active** User, or (Super admin only) an **active** Admin. Refused
  (403): a Super admin, whoever asks. Refused (422): oneself (Settings), an invited,
  deactivated or pseudonymised account.
- The administrator never learns or chooses a password. The account's **current password stops
  working** at once (replaced by a random one nobody knows, as for an Admin reactivated by
  another, R126), every session and trusted browser ends (`session_epoch`), pending
  credential links are revoked, and a **single-use link with the usual short life**
  (`PASSWORD_RESET_TTL_S`) is emailed to the account's **own verified address**: neither
  administrator can change an administrator's address (ADR-0030), so this is no way into an
  Admin's account. The second factor is untouched; the owner completes the reset through the
  existing `password-reset/confirm`, which applies the password policy and breached-password
  check and ends sessions again.
- The account's address also gets a notice that an administrator reset the password (never
  the link or a password), and the link's own email says so (the owner's own "Forgot password"
  email advises ignoring an unexpected message; this one would be wrong advice, as the old
  password no longer works).
- Audited `auth.password_reset_by_admin` (actor, target, count of revoked links) and
  `auth.password_reset_issued` (`reason: administrator`). At most 3 administrator-issued links
  per account per hour (429, `Retry-After` the time until the oldest leaves the hour), so the
  owner's mailbox isn't flooded. They are counted apart from the owner's own Forgot-password
  requests, which they never use up.
- The existing *Set temporary password* (`set-password`) is unchanged: users in reach only,
  never an administrator's.

### Delete account: reassign, deactivate, erase identity, in one use case
`GET /api/v1/admin/users/{id}/deletion-preview` (a read: the permission and reach of the
operations, no step-up, no throttle beyond the API's) and
`POST /api/v1/admin/users/{id}/delete` `{version, confirm_email, reassign_to}` (the same
permission and reach, a recent second factor where the role requires one, the `auth`
throttle). "Delete" keeps the project's lifecycle: the CRM business data is **never
deleted**; the account becomes a pseudonymised former user (`privacy.staff`, ADR-0032), which
is the erasure the privacy design already defines.

**Locking (adversarial review, M1 and M2).** The first version ran everything in one
transaction under the hold barrier, the user-administration lock and the target's row. That
reversed the project's lock order (lead, then user rows FOR SHARE as leaf locks) and deadlocked
with any write on the target's leads or pipelines, and held the global locks for a whole batch
of reassignments (5 s and more; `lock_timeout` is 5 s), failing other user administration and
hold placement. So the work is split, and **no lock is held across the hand-over**:

1. **Decide, write nothing.** The actor and target are read (404 outside the actor's reach);
   refused: oneself (422), a Super admin (403), an already pseudonymised account (422), a
   stale `version` (409), a wrong `confirm_email` (422), and a **legal hold** on the person
   (409 `legal_hold` for a caller with privacy.manage; a plain 422 for an Admin, and the
   preview shows no hold to them: R148).
2. **Hand over the work**, if the account has any: the leads that are not archived, and
   archived ones still carrying open work (otherwise the account could never be deleted),
   go to `reassign_to` through `leads.services.reassign_lead` (audited; the open deals,
   tasks, meetings and notes follow the lead), **one lead per transaction** holding only its
   own locks, with the actor and target re-read before each and a lead edited at that moment
   skipped for the next round. The new owner must be active and within the caller's reach
   (an Admin: themselves or an active member of their team; 400 `details.reassign_to`
   otherwise). Won/lost deals and finished work keep the person who did them. A request
   hands over at most `MAX_LEADS_PER_REQUEST` leads or `TIME_BUDGET_S` seconds, and answers
   **202** `{completed: false, remaining_leads}`; the client repeats the same request.
3. **Archive their personal pipelines** that hold no open deal (kept, never deleted); one still
   holding a deal (now somebody else's) stays active so that deal keeps its board.
4. **Finish in one short transaction**, in privacy.staff's order: the hold barrier, the
   user-administration lock, the account's row (last, after every CRM lock is gone). Everything
   is decided again on the locked rows, the remaining work counted again (a lead that arrived
   meanwhile: nothing changes and the answer is 202, or 400 if no `reassign_to` was sent);
   then the account is **deactivated** (every session ends; an Admin's team is released: its
   members answer to no Admin until a Super admin assigns them, as when an Admin is demoted;
   pending invitations they sent and the temporary passwords they set go with them,
   ADR-0035) and its identity **erased** (`privacy.staff.erase_locked`), recorded in the
   erasure ledger so a restored backup is erased again.
5. **What is erased depends on the actor** (review, M3). The user's name and email can be
   chosen by the Admin who creates or renames them, and erasure matches them across the
   organisation's Ask Arkray conversations and audit details. So when the actor lacks
   `privacy.manage` (an Admin) the erasure is **limited to the account's own traces**: its
   own conversations and the audit details of events it caused; nothing someone else wrote or
   did about it. A Super admin's deletion erases everything the privacy step does. The audit
   says which (`user.deleted`, `erasure`).

Audit events stay, with the ids of the actor and the target, and the CRM records keep their
owner and attribution ids; the trail gains `user.deleted` (actor, target, role, counts) next to
`user.deactivated` (`reason: deleted`) and `user.pseudonymised` (`deleted: true`). Irreversible:
the UI asks the administrator to type the account's email. A burst of deletions raises an alert
(`ArkrayAccountsDeleted`).

### Authorization is one function
Both operations ask the same pair of checks of the locked target: reach (`_reaches`, the
team rule of ADR-0035) and a **deletable/resettable role** (`team_admin` or `sales_user`, and
`team_admin` only for a Super admin). A new role is therefore refused until it is added there.

## Consequences
- A Super admin can offboard an Admin or a User completely from the Users page; an Admin can
  offboard their own team's users. Nothing is silently removed from the CRM.
- ADR-0030's rule that no API caller erases an administrator's account is relaxed for one case:
  a Super admin may delete an **Admin's** account (through user administration, after the
  hand-over). The privacy endpoint `pseudonymise` is unchanged and still refuses every
  administrator, and a Super admin's account is still operator-only.
- Large accounts are handed over in rounds of `MAX_LEADS_PER_REQUEST` leads or
  `TIME_BUDGET_S` seconds (about 200 leads in 4 to 6 s on a laptop); a partly handed-over
  account stays active, and leads already moved stay moved if the administrator stops. The
  `auth` throttle (20 requests a minute) is no limit in practice (a round takes seconds); an
  account of many thousands of leads is finished after the next minute.
- After a restore from a backup older than a deletion, the hand-over, the archived pipelines
  and an Admin's released team are not in the erasure ledger: the identity is erased again,
  the work is back with the former user ([privacy.md](../privacy.md#deleting-an-account-adr-0036)).
- An Admin's deletion has no step-up (two-step verification is required of Super admins
  only, ADR-0033), no undo window and nothing the owner is told; the alert on bursts and the
  security events are the safeguard. A hijacked Admin session can already deactivate the
  team and set temporary passwords, which is worse.
- Resetting an Admin's password cannot be abused to take the account over: the link goes
  only to the Admin's own mailbox, whose address no other administrator can change.

## Alternatives considered
- **A hard `DELETE` of the user row**: breaks attribution of every owned, created or closed
  record and the append-only audit trail, and is impossible under the foreign keys.
- **A temporary password for Admins**: the Super admin would know a credential for another
  administrator (ADR-0030, R100).
- **One transaction for any number of leads**: holds the user-administration lock and a
  request for as long as thousands of reassignments take.
