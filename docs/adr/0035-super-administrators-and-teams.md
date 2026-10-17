# 0035. Super administrators and team administrators

Status: Accepted (product decisions confirmed by the owner on 2026-10-13); amends
[0030](0030-administrator-accounts-change-only-by-their-owner.md) (a super administrator may
change a team administrator's role) and [0033](0033-administrator-mfa-and-reactivation.md)
(team administrators are administrators for two-step verification and reactivation); extended by [0036](0036-account-deletion-and-administrator-password-reset.md) (delete account, emailed reset link)
Date: 2026-10-13

## Context
Arkray CRM had two roles: `admin` (organisation-wide: every user's CRM, user administration,
configuration, security events, privacy requests) and `sales_user`. The owner asked for a
hierarchy: a **super admin** who can create users and admins and manages everything, and
**admins** who create users and manage only their own users.

The owner decided (2026-10-13):
1. An Admin sees and works on **only their team**: their own records and their users'. Only
   the Super Admin sees the whole organisation.
2. Organisation-wide areas stay **Super Admin only**: pipeline, stage and custom-field
   configuration of shared pipelines; security events and the audit log; privacy requests
   (exports, pseudonymisation).
3. **Existing administrators become Super Admins.** Existing users start with no Admin, so
   only the Super Admins manage them until they are assigned.
4. The Super Admin controls Admins' **role and teams, not their credentials**: an Admin's
   password, email and second factor are still changed only by that Admin (or an operator),
   so nobody can sign in as an Admin.

## Decision

### Roles
| Role value | Label | Who |
|---|---|---|
| `admin` | Super admin | the organisation-wide administrator; capabilities unchanged |
| `team_admin` | Admin | an administrator of their team |
| `sales_user` | User | unchanged |

The stored value `admin` keeps its meaning and its powers, so the upgrade changes no data and
every existing administrator is a super administrator. The new role has a new value the
previous release doesn't know: during a rolling deploy it grants nothing there (fail closed)
rather than an old pod reading a new team administrator as an organisation-wide one.

### Teams
`identity_user.manager` names the team administrator who manages a **sales user**, or is NULL
(managed by the super administrators only). CHECKs: only a sales user has a manager; never
oneself. That the manager is a team administrator (active or invited, not pseudonymised) is
kept by `identity.services` under the user-administration lock; every reader also asks for the
member's role and the actor's team capability (`identity.teams`), so a reference left behind
grants nothing.

**Reach**: a super administrator reaches everyone; a team administrator reaches themselves and
their team's sales users (any status: a deactivated member's history stays viewable); anyone
else reaches only themselves.

### Capabilities
The team administrator has distinct capabilities, never the organisation-wide ones, so any
check not written for teams denies them (fail closed):

| Organisation-wide (super admin) | Team counterpart (admin) |
|---|---|
| `crm.view_all`: workspace `all` = organisation | `crm.view_team`: workspace `all` = their team (scope `TEAM`) |
| `workspace.view_any` | `workspace.view_team`: a team member's workspace |
| `crm.assign_any` | `crm.assign_team`: assign within the team (themselves included) |
| `crm.manage_any` | `crm.manage_team`: write in a team member's workspace or the team's |
| `users.manage` | `users.manage_team`: create and manage the team's sales users |
| `support.access` | `support.access_team`: support sessions with team members |
| `config.manage`, `audit.view`, `privacy.manage` | none |

Both hold `crm.access_own` and `ai.query`. Routes open to both name both
(`requires_any`, matrix rule `capability:a|b`); the service then applies the caller's reach.

### The team scope
`AccessScope.team(actor, members)`: kind `TEAM`, owners = the actor and their team's sales
users. It filters like a user's scope (`owner_id IN …`); it is never organisation-wide; it has
no single subject (`subject_user_id` raises, so code written for one owner can't read "no
subject" as "everyone"). Workspace `all` resolves to it for a team administrator, `{uuid}` only
to a team member (404 otherwise, identical to a user who doesn't exist). Writes in it need
`crm.manage_team` and every owner still in the team (`authorize_write` re-reads the team).
Creating a record there names an owner in the team; owner filters are allowed (intersected
with the scope). Pipeline configuration is refused in the team scope: a member's personal
pipeline is configured from their workspace, shared pipelines by super administrators.

### User administration
- **Listing and reading**: a super administrator sees everyone; a team administrator sees
  themselves and their team (404 for anyone else).
- **Creating**: a team administrator creates **users only**, in their own team. A super
  administrator creates users (optionally naming their Admin), Admins and Super admins.
  Administrators of either kind are always invited (no initial password).
- **Role changes** (super administrators only): a user may be promoted to Admin or Super admin
  (with the existing guards: their own password chosen, no credential change by another
  administrator in the last 7 days); an Admin may be made a User or a Super admin. Another
  **super** administrator's role is still never changed (ADR-0030: deactivate instead). Nobody
  changes their own role. An Admin who stops being an Admin leaves their team unassigned
  (members' manager → NULL, counted in the audit) and their pending invitations revoked.
- **Moving a user between teams** (super administrators only): `PATCH {manager}` with an active
  or invited Admin, or null; audited `user.manager_changed` (from, to).
- **Credentials** (email, password, second-factor reset): never for another administrator of
  either kind (ADR-0030), and not for an account that stopped being an administrator through
  another's action within `ADMIN_PROMOTION_COOLING_OFF_S` (7 days; "demote-then-unshield",
  the mirror of promote-then-shield).
- **Deactivating, reactivating, resending invitations, clearing a sign-in lock**: within reach.
  A team administrator can't reach another administrator, so only super administrators
  off-board Admins. An Admin reactivated by another comes back with no known password (R126).
- **Last administrator**: at least one active **super** administrator always remains.
- Two-step verification is required for both administrator roles where required for
  administrators (`identity.policy.administrator_roles`).

### Everything else
- Support sessions: a team administrator only with their team's members; the session ends at
  the next request once the member leaves the team.
- Assignees (owner pickers): a team administrator sees themselves and their team's active
  members.
- Correcting a closed deal: `crm.manage_any`, or `crm.manage_team` for a deal in the team.
- Dashboard, search and Ask Arkray consume the scope: a team administrator's "all" covers the
  team. Ask Arkray conversations in it have the kind `team`.
- Security events, the audit log, privacy requests, shared pipeline configuration and every
  operator command that names an administrator: super administrators only.
- A team change takes effect at the next request (like every permission change); a write
  already authorised in flight completes.

### Added by the independent review (2026-10-13)
- **Temporary passwords leave with their setter's reach**: when an administrator loses reach
  over an account (the user moves to another team or to none, the Admin stops being an
  administrator, or any administrator is deactivated), every account still on a temporary
  password that administrator set gets a random password nobody knows, its sessions and reset
  links end, and a reset link goes to its own address (`auth.temporary_password_voided`, in the
  security events). Otherwise an Admin could sign in as a user who had left their team.
- **Invited accounts are never promoted**, and an account an Admin created counts toward the
  7-day promotion cooling-off (`user.created` with `creator_scope: "team"`): otherwise an Admin
  could invite an address they read and have it promoted at once.
- **User rows are locked `FOR NO KEY UPDATE`** in user administration, never `FOR UPDATE`. A
  role change locks the Admin's row and then updates her members' rows, while her CRM write
  share-locks a member's row and, at commit, key-share-locks her own row through its foreign
  keys: with `FOR UPDATE` the two deadlocked. User ids never change, so `NO KEY` loses nothing.
- **Rolling deploy**: the previous release grants `team_admin` nothing, but it also doesn't
  treat it as an administrator (it would let a super admin change an Admin's credentials,
  skip R126 and leave invitations live). Create or promote Admins only once every pod runs this
  release ([deployment.md](../deployment.md)).
- **Messages**: in a team's view, a deal whose customer moved out of the team says to ask a
  super admin (the team's view can't reach it); in a member's workspace it points to the team's
  view.

## Consequences
- The organisation can delegate day-to-day user management and oversight to Admins without
  giving them the whole organisation's data or its configuration.
- Every check written before teams denies team administrators until it is made team-aware.
- Users assigned to no Admin are visible only to super administrators; reassigning a deal to
  someone outside the team is a super administrator's job.
- An Admin's team is not reassigned automatically when the Admin is deactivated (their users
  stay listed under them, managed by super administrators meanwhile); demotion unassigns it.
- Open (risk register): team-scope list pages grow with the organisation's newer records as
  well as the team's (R172; a per-owner merge when it matters), and a legal hold on a member
  doesn't keep their Admin's team conversations about them, as for organisation-wide ones
  (R175). Accepted: an Admin can learn that an email has an account (R171); a team change
  applies from the next request (R173); a deactivated Admin keeps their team (R174).

## Alternatives considered
- **Reuse the organisation-wide capabilities with a narrower scope**: fewer strings, but any
  check that forgot the reach would hand a team administrator the organisation (fail open).
- **Rename the stored value** (`admin` → `super_admin`): a data migration, and during a rolling
  deploy the previous release would read every new team administrator (stored as `admin`) as
  organisation-wide.
- **Teams as a separate table** (many admins per team, nested teams): more than asked; the
  `manager` reference can grow into it.
