import { describe, expect, it } from "vitest";

import type { ViewerDto } from "@/lib/api/types";
import { adminViewer, makeViewer, TEAM_ADMIN_CAPABILITIES, teamAdminViewer } from "@/test/fixtures";

import {
  canAssign,
  canManageAdmins,
  canManageOthersRecords,
  canManageUsers,
  canOpenUserWorkspaces,
  canStartSupport,
  type Capability,
  hasCapability,
  initials,
  overviewKind,
  toViewer,
  type Viewer,
} from "./viewer";

describe("initials", () => {
  it.each([
    ["Rahul Sharma", "RS"],
    ["Priya", "P"],
    ["  amit  kumar singh ", "AS"],
    ["", ""],
  ])("%s -> %s", (name, expected) => {
    expect(initials(name)).toBe(expected);
  });
});

describe("hasCapability", () => {
  const viewer: Viewer = makeViewer({ id: "u1", firstName: "Priya", lastName: "", email: "p@x.test", capabilities: ["crm.access_own"] });

  it("checks the viewer's capability list", () => {
    expect(hasCapability(viewer, "crm.access_own")).toBe(true);
    expect(hasCapability(viewer, "users.manage")).toBe(false);
  });

  it("grants nothing while the viewer is unknown", () => {
    expect(hasCapability(null, "crm.access_own")).toBe(false);
  });
});

const VIEWER_DTO_REQUIRED = {
  id: "a1",
  email: "a@x.test",
  first_name: "Anita",
  last_name: "Rao",
  full_name: "Anita Rao",
  role: "admin" as const,
  role_label: "Admin",
  capabilities: ["users.manage"],
  features: { ask: false },
  password_change_required: false,
  mfa_enabled: false,
  mfa_enrollment_required: true,
  support_session: null,
};

describe("toViewer", () => {
  it("maps the API shape and ignores capabilities the UI doesn't know", () => {
    const viewer = toViewer({
      id: "u1",
      email: "p@x.test",
      first_name: "Priya",
      last_name: "Patel",
      full_name: "Priya Patel",
      role: "sales_user",
      role_label: "User",
      capabilities: ["crm.access_own", "superpowers.all"],
      features: { ask: true },
      password_change_required: false,
      mfa_enabled: true,
      mfa_enrollment_required: false,
      support_session: null,
    });
    expect(viewer).toEqual({
      id: "u1",
      email: "p@x.test",
      firstName: "Priya",
      lastName: "Patel",
      fullName: "Priya Patel",
      roleLabel: "User",
      capabilities: ["crm.access_own"],
      features: { ask: true },
      passwordChangeRequired: false,
      mfaEnabled: true,
      mfaEnrollmentRequired: false,
      supportSession: null,
    });
  });

  it("reads two-step verification as off and not required from a backend without it", () => {
    // An older backend's response (the generated schema now requires both fields).
    const older = {
      id: "u1",
      email: "p@x.test",
      first_name: "Priya",
      last_name: "Patel",
      full_name: "Priya Patel",
      role: "admin",
      role_label: "Admin",
      capabilities: [],
      features: { ask: false },
      password_change_required: false,
      support_session: null,
    } as const;
    const viewer = toViewer(older as unknown as ViewerDto);
    expect(viewer).toMatchObject({ mfaEnabled: false, mfaEnrollmentRequired: false });
    expect(toViewer({ ...VIEWER_DTO_REQUIRED })).toMatchObject({ mfaEnabled: false, mfaEnrollmentRequired: true });
  });

  it("maps a support session (the target's id in its canonical spelling)", () => {
    const viewer = toViewer({
      id: "a1",
      email: "a@x.test",
      first_name: "Anita",
      last_name: "Rao",
      full_name: "Anita Rao",
      role: "admin",
      role_label: "Admin",
      capabilities: ["support.access"],
      features: { ask: false },
      password_change_required: false,
      mfa_enabled: true,
      mfa_enrollment_required: false,
      support_session: {
        id: "s1",
        target: { id: "3F2B8C1E-9A4D-4E2F-8B7A-1C2D3E4F5A6B", full_name: "Rahul Sharma" },
        reason: "Fixing a lead",
        started_at: "2026-10-05T10:00:00Z",
        expires_at: "2026-10-05T10:30:00Z",
      },
    });
    expect(viewer.supportSession).toEqual({
      id: "s1",
      target: { id: "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b", fullName: "Rahul Sharma" },
      reason: "Fixing a lead",
      startedAt: "2026-10-05T10:00:00Z",
      expiresAt: "2026-10-05T10:30:00Z",
    });
  });
});

describe("super administrators and team administrators (ADR-0035)", () => {
  const superAdmin = adminViewer;
  const teamAdmin = teamAdminViewer;
  const user = makeViewer();

  it("names each power by what it offers, held organisation-wide or for a team", () => {
    expect([superAdmin, teamAdmin, user, null].map(canManageUsers)).toEqual([true, true, false, false]);
    expect([superAdmin, teamAdmin, user, null].map(canOpenUserWorkspaces)).toEqual([true, true, false, false]);
    expect([superAdmin, teamAdmin, user, null].map(canAssign)).toEqual([true, true, false, false]);
    expect([superAdmin, teamAdmin, user, null].map(canManageOthersRecords)).toEqual([true, true, false, false]);
    expect([superAdmin, teamAdmin, user, null].map(canStartSupport)).toEqual([true, true, false, false]);
  });

  it("roles, teams and administrators' accounts are the super administrator's only", () => {
    expect([superAdmin, teamAdmin, user, null].map(canManageAdmins)).toEqual([true, false, false, false]);
  });

  it("each team capability alone is enough for its helper (no hidden dependency on another)", () => {
    const only = (capability: Capability) => makeViewer({ capabilities: [capability] });
    expect(canManageUsers(only("users.manage_team"))).toBe(true);
    expect(canOpenUserWorkspaces(only("workspace.view_team"))).toBe(true);
    expect(canAssign(only("crm.assign_team"))).toBe(true);
    expect(canManageOthersRecords(only("crm.manage_team"))).toBe(true);
    expect(canStartSupport(only("support.access_team"))).toBe(true);
    // ...and none of them is the super administrator's.
    expect(canManageAdmins(only("users.manage_team"))).toBe(false);
  });

  it("the overview is the organisation (crm.view_all), the team (crm.view_team), or none", () => {
    expect(overviewKind(superAdmin)).toBe("organization");
    expect(overviewKind(teamAdmin)).toBe("team");
    expect(overviewKind(user)).toBeNull();
    expect(overviewKind(null)).toBeNull();
    // Organisation-wide wins if a viewer somehow had both.
    expect(overviewKind(makeViewer({ capabilities: ["crm.view_team", "crm.view_all"] }))).toBe("organization");
  });

  it("a team administrator holds no organisation-wide capability", () => {
    for (const capability of ["crm.view_all", "workspace.view_any", "crm.assign_any", "crm.manage_any", "users.manage", "support.access", "config.manage", "audit.view", "privacy.manage"] as const) {
      expect(hasCapability(teamAdmin, capability)).toBe(false);
    }
  });

  it("keeps the six team capabilities from the API (they are known strings now)", () => {
    const viewer = toViewer({
      ...VIEWER_DTO_REQUIRED,
      role: "team_admin",
      role_label: "Admin",
      capabilities: ["crm.access_own", "crm.view_team", "workspace.view_team", "crm.assign_team", "crm.manage_team", "users.manage_team", "support.access_team", "ai.query"],
    });
    expect(viewer.roleLabel).toBe("Admin");
    expect([...viewer.capabilities].sort()).toEqual([...TEAM_ADMIN_CAPABILITIES].sort());
  });
});
