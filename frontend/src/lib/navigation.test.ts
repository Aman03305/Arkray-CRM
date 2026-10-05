import { describe, expect, it } from "vitest";

import { makeViewer } from "@/test/fixtures";

import { administrationNavigation, navigationWorkspace, showsSettings, supportSessionPath, workspaceNavigation } from "./navigation";
import type { Viewer } from "./viewer";

const salesUser: Viewer = makeViewer({ id: "u1", firstName: "Priya", lastName: "", email: "p@x.test", capabilities: ["crm.access_own"] });
const admin: Viewer = makeViewer({ id: "a1", firstName: "Admin", lastName: "", email: "a@x.test", capabilities: ["crm.access_own", "users.manage"] });

describe("navigation", () => {
  it("offers exactly the four CRM modules", () => {
    expect(workspaceNavigation({ kind: "self" }).map((i) => i.label)).toEqual([
      "Dashboard",
      "Pipeline",
      "Leads",
      "Activities",
    ]);
  });

  it("never includes Companies or Products", () => {
    const labels = [
      ...workspaceNavigation({ kind: "self" }),
      ...administrationNavigation(admin),
    ].map((i) => i.label.toLowerCase());
    expect(labels).not.toContain("companies");
    expect(labels).not.toContain("products");
  });

  it("shows Users only to viewers who can manage users", () => {
    expect(administrationNavigation(admin).map((i) => i.href)).toEqual(["/admin/users"]);
    expect(administrationNavigation(salesUser)).toEqual([]);
    expect(administrationNavigation(null)).toEqual([]);
  });
});

describe("during a support session", () => {
  const TARGET = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b";
  const OTHER = "5c4b3a29-1807-4f6e-9d5c-4b3a29180716";
  const session = {
    id: "s1",
    target: { id: TARGET, fullName: "Rahul Sharma" },
    reason: "",
    startedAt: "2026-10-05T10:00:00Z",
    expiresAt: "2026-10-05T10:30:00Z",
  };
  const supporting = makeViewer({
    id: "a1",
    capabilities: ["crm.access_own", "crm.view_all", "workspace.view_any", "users.manage", "support.access"],
    supportSession: session,
  });

  it("keeps every module link in the supported user's workspace", () => {
    expect(navigationWorkspace("/leads", supporting)).toEqual({ kind: "user", userId: TARGET });
    expect(navigationWorkspace(`/admin/users/${OTHER}/leads`, supporting)).toEqual({ kind: "user", userId: TARGET });
  });

  it("hides Users and Settings", () => {
    expect(administrationNavigation(supporting)).toEqual([]);
    expect(showsSettings(supporting)).toBe(false);
    expect(showsSettings(makeViewer())).toBe(true);
  });

  it.each([
    ["/dashboard", "dashboard"],
    ["/leads", "leads"],
    ["/leads/9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2c", "leads"],
    ["/pipeline", "pipeline"],
    ["/activities", "activities"],
    ["/ask", "ask"],
    ["/admin", "dashboard"],
    ["/admin/users", "dashboard"],
    ["/settings", "dashboard"],
    [`/admin/users/${OTHER}/pipeline`, "pipeline"],
    [`/admin/users/${OTHER}/leads/9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2c`, "leads"],
  ])("sends %s to the supported user's %s", (path, section) => {
    expect(supportSessionPath(path, session)).toBe(`/admin/users/${TARGET}/${section}`);
  });

  it("leaves the supported user's own pages alone", () => {
    expect(supportSessionPath(`/admin/users/${TARGET}/leads`, session)).toBeNull();
    expect(supportSessionPath(`/admin/users/${TARGET.toUpperCase()}/pipeline/new`, session)).toBeNull();
  });
});
