import { describe, expect, it } from "vitest";

import { makeViewer } from "@/test/fixtures";

import { administrationNavigation, workspaceNavigation } from "./navigation";
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
