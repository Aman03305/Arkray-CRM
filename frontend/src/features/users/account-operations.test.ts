/**
 * Who may reset a password or delete an account, for every combination of viewer, role, status,
 * "is it you" and "is it deleted" (ADR-0036). The pages repeat a few of these through the screen
 * (UserPasswordReset.test.tsx, UserDeletion.test.tsx); this is the whole table, with no screen.
 */
import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/client";
import type { AdminUser, Role } from "@/lib/api/types";
import { makeAdminUser } from "@/test/fixtures";

import {
  canDeleteAccount,
  canResetPassword,
  describeAccountError,
  inactiveReason,
  isDeleted,
  type Operator,
  signInToolNotes,
} from "./account-operations";

const ROLES: Role[] = ["admin", "team_admin", "sales_user"];
const STATUSES = ["active", "invited", "deactivated"] as const;

const SUPER: Pick<Operator, "manages" | "superAdmin"> = { manages: true, superAdmin: true };
const ADMIN: Pick<Operator, "manages" | "superAdmin"> = { manages: true, superAdmin: false };
const NOBODY: Pick<Operator, "manages" | "superAdmin"> = { manages: false, superAdmin: false };

/** What each viewer may act on, written out: no one acts on a Super admin. */
const ACTS_ON: Record<string, readonly Role[]> = {
  super: ["team_admin", "sales_user"],
  admin: ["sales_user"],
  nobody: [],
};
const VIEWERS = { super: SUPER, admin: ADMIN, nobody: NOBODY } as const;

function account(role: Role, status: (typeof STATUSES)[number], deleted: boolean): AdminUser {
  return makeAdminUser({
    role,
    status,
    pseudonymised_at: deleted ? "2026-10-02T09:00:00Z" : null,
  });
}

describe("canDeleteAccount and canResetPassword", () => {
  const cases = Object.entries(VIEWERS).flatMap(([viewer, powers]) =>
    ROLES.flatMap((role) =>
      STATUSES.flatMap((status) =>
        [false, true].flatMap((deleted) => [false, true].map((isSelf) => ({ viewer, powers, role, status, deleted, isSelf }))),
      ),
    ),
  );

  it("covers every combination", () => {
    expect(cases).toHaveLength(3 * 3 * 3 * 2 * 2);
  });

  it.each(cases)("$viewer viewer, $role $status, deleted=$deleted, self=$isSelf", ({ viewer, powers, role, status, deleted, isSelf }) => {
    const target = account(role, status, deleted);
    const who: Operator = { isSelf, ...powers };
    // Their own account and a deleted one: never. Otherwise by role: a Super admin's: never.
    const deletable = !isSelf && !deleted && (ACTS_ON[viewer] ?? []).includes(role);
    expect(canDeleteAccount(target, who)).toBe(deletable);
    // An emailed link needs a password to reset: only an active account has one.
    expect(canResetPassword(target, who)).toBe(deletable && status === "active");
  });
});

describe("isDeleted", () => {
  it("is the account's pseudonymisation, whatever its status says", () => {
    expect(isDeleted(account("sales_user", "deactivated", true))).toBe(true);
    expect(isDeleted(account("sales_user", "deactivated", false))).toBe(false);
  });
});

describe("inactiveReason", () => {
  it.each([
    ["invited", "Not available until they accept their invitation."],
    ["deactivated", "Not available for deactivated users."],
    ["active", null],
  ] as const)("%s", (status, reason) => {
    expect(inactiveReason({ status })).toBe(reason);
  });
});

describe("signInToolNotes", () => {
  const BLOCKED = "Not available for administrators.";
  const superAdmin: Operator = { isSelf: false, ...SUPER };

  it("says nothing when nothing is blocked", () => {
    expect(signInToolNotes(account("sales_user", "active", false), superAdmin, null)).toEqual([]);
  });

  it("repeats the one reason as it is for anyone who gets no password tools at all", () => {
    for (const role of ["admin", "sales_user"] as const) {
      expect(signInToolNotes(account(role, "invited", false), superAdmin, BLOCKED)).toEqual([BLOCKED]);
    }
    // Neither does an Admin's own page: they reach no other Admin.
    expect(signInToolNotes(account("team_admin", "active", false), { isSelf: false, ...ADMIN }, BLOCKED)).toEqual([BLOCKED]);
    expect(signInToolNotes(account("team_admin", "active", false), { isSelf: true, ...SUPER }, BLOCKED)).toEqual([BLOCKED]);
    expect(signInToolNotes(account("team_admin", "active", true), superAdmin, BLOCKED)).toEqual([BLOCKED]);
  });

  it("labels what is not offered for an Admin a Super admin may reset, and why a reset isn't offered", () => {
    expect(signInToolNotes(account("team_admin", "active", false), superAdmin, BLOCKED)).toEqual([
      "Temporary password and support access: Not available for administrators.",
    ]);
    expect(signInToolNotes(account("team_admin", "invited", false), superAdmin, BLOCKED)).toEqual([
      "Temporary password and support access: Not available for administrators.",
      "Reset password: Not available until they accept their invitation.",
    ]);
    expect(signInToolNotes(account("team_admin", "deactivated", false), superAdmin, BLOCKED)).toEqual([
      "Temporary password and support access: Not available for administrators.",
      "Reset password: Not available for deactivated users.",
    ]);
  });
});

describe("describeAccountError", () => {
  const error = (status: number, code: string, message: string, retryAfter: number | null = null) =>
    new ApiError(status, code, message, null, "req-1", retryAfter);

  it("keeps the server's explanation of a refused account (403)", () => {
    expect(describeAccountError(error(403, "permission_denied", "A super admin's account is managed only by that super admin."), "requests")).toEqual({
      message: "A super admin's account is managed only by that super admin.",
      requestId: "req-1",
    });
  });

  it("uses the app's own words for everything else", () => {
    expect(describeAccountError(error(403, "mfa_required", "x"), "requests").message).toBe(
      "Confirm it's you with a code from your authenticator app to do that.",
    );
    expect(describeAccountError(error(429, "rate_limited", "Request was throttled.", 30), "reset links").message).toBe(
      "Too many reset links. Try again in about a minute.",
    );
    expect(describeAccountError(error(422, "business_rule_violation", "Only active accounts can be sent a reset link."), "requests").message).toBe(
      "Only active accounts can be sent a reset link.",
    );
    expect(describeAccountError(new Error("boom"), "requests")).toEqual({ message: "Something went wrong. Please try again.", requestId: null });
  });
});
