import { describe, expect, it } from "vitest";

import { makeViewer } from "@/test/fixtures";

import { hasCapability, initials, toViewer, type Viewer } from "./viewer";

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
      supportSession: null,
    });
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
