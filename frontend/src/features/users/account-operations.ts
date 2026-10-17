/**
 * Which account operations the Users page offers for an account, and what it says about the
 * ones it doesn't (ADR-0036: a reset link emailed to the account, and deleting it). The UI only
 * decides what to show: the API applies the viewer's reach and the account's role and status
 * again and refuses everything else (404 outside the reach, 403 for a Super admin's account).
 *
 * Who may act on whom (the ADR's matrix):
 *   - a Super admin (users.manage): Users and Admins;
 *   - an Admin (users.manage_team): the Users of their own team, which is all their list holds;
 *   - nobody: a Super admin's account, their own, or a deleted one.
 */
import { describeError, type DisplayError, isApiError } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";

/** Who is asking, as far as these operations care. */
export interface Operator {
  /** The account is the viewer's own: they change their own password in Settings. */
  isSelf: boolean;
  /** users.manage or users.manage_team: the Users page is theirs at all. */
  manages: boolean;
  /** users.manage: a Super admin. An Admin acts on the Users of their team only. */
  superAdmin: boolean;
}

/** Someone who may act on no account (a viewer who can't manage users). */
export const NO_POWERS = { manages: false, superAdmin: false } as const;

/**
 * A deleted account: pseudonymised for good (ADR-0036), shown as a "Former user" with the
 * status "Deleted". It is never changed again, and the API refuses every operation on it.
 */
export function isDeleted(user: Pick<AdminUser, "pseudonymised_at">): boolean {
  return Boolean(user.pseudonymised_at);
}

/**
 * The roles an operation may touch, decided in one place (as on the server): a User, or, for a
 * Super admin only, an Admin. Never a Super admin's account, whoever asks. Deny by default: a
 * role added later is not offered until it is added here.
 */
function operableRole(user: Pick<AdminUser, "role">, superAdmin: boolean): boolean {
  return user.role === "sales_user" || (user.role === "team_admin" && superAdmin);
}

/** Delete the account: a User or Admin in the viewer's reach, in any status. */
export function canDeleteAccount(user: AdminUser, who: Operator): boolean {
  return who.manages && !who.isSelf && !isDeleted(user) && operableRole(user, who.superAdmin);
}

/** Email the account a reset link: the same accounts, but only an active one has a password to reset. */
export function canResetPassword(user: AdminUser, who: Operator): boolean {
  return canDeleteAccount(user, who) && user.status === "active";
}

export const INVITED_REASON = "Not available until they accept their invitation.";
export const DEACTIVATED_REASON = "Not available for deactivated users.";

/** Why a status stops the password tools (null for an active account). */
export function inactiveReason(user: Pick<AdminUser, "status">): string | null {
  if (user.status === "invited") return INVITED_REASON;
  if (user.status === "deactivated") return DEACTIVATED_REASON;
  return null;
}

/**
 * What the details panel says about the sign-in tools it doesn't offer. `supportBlocked` is why
 * a temporary password and support access aren't (the user is the viewer, an administrator,
 * deactivated...). For an Admin that a Super admin may reach, the panel does offer a reset
 * link, so each tool's reason is labelled and says what it is about.
 */
export function signInToolNotes(user: AdminUser, who: Operator, supportBlocked: string | null): string[] {
  if (!supportBlocked) return [];
  const resettableAdmin = who.superAdmin && user.role === "team_admin" && !who.isSelf && !isDeleted(user);
  if (!resettableAdmin) return [supportBlocked];
  const notes = [`Temporary password and support access: ${supportBlocked}`];
  const inactive = inactiveReason(user);
  if (inactive) notes.push(`Reset password: ${inactive}`);
  return notes;
}

/**
 * An account operation's failure in words for its dialog. A refusal of the account itself (403:
 * a Super admin's) keeps the server's own explanation; the rest are the app's usual messages.
 * `tooMany`: what a 429 refused, for the generic "Too many ... Try again ..." sentence.
 */
export function describeAccountError(error: unknown, tooMany: string): DisplayError {
  if (isApiError(error, 403, "permission_denied") && error.message) {
    return { message: error.message, requestId: error.requestId };
  }
  return describeError(error, { tooMany });
}
