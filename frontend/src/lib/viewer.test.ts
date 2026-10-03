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
    });
    expect(viewer).toEqual({
      id: "u1",
      email: "p@x.test",
      firstName: "Priya",
      lastName: "Patel",
      fullName: "Priya Patel",
      roleLabel: "User",
      capabilities: ["crm.access_own"],
    });
  });
});
