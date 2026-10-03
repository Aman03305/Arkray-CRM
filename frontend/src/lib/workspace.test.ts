import { describe, expect, it } from "vitest";

import { makeViewer } from "@/test/fixtures";

import type { Viewer } from "./viewer";
import {
  activeSection,
  leadHref,
  newLeadHref,
  workspaceApiPath,
  workspaceFromPathname,
  workspaceHref,
  type Workspace,
} from "./workspace";

const RAHUL = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b";
const salesUser: Viewer = makeViewer({ id: "u1", firstName: "Priya", lastName: "Patel", email: "p@x.test", capabilities: ["crm.access_own", "ai.query"] });
const admin: Viewer = makeViewer({ id: "a1", firstName: "Admin", lastName: "", email: "a@x.test", capabilities: ["crm.access_own", "crm.view_all", "workspace.view_any", "users.manage"] });

describe("workspaceFromPathname", () => {
  it("treats top-level module routes as the sales user's own workspace", () => {
    expect(workspaceFromPathname("/leads", salesUser)).toEqual({ kind: "self" });
  });

  it("treats top-level module routes as organisation-wide for viewers with crm.view_all", () => {
    expect(workspaceFromPathname("/pipeline", admin)).toEqual({ kind: "organization" });
  });

  it("recognises an admin user-workspace route and keeps the selected user", () => {
    expect(workspaceFromPathname(`/admin/users/${RAHUL}/pipeline`, admin)).toEqual({ kind: "user", userId: RAHUL });
    expect(workspaceFromPathname(`/admin/users/${RAHUL}`, admin)).toEqual({ kind: "user", userId: RAHUL });
  });

  it("ignores malformed user ids rather than building API paths from them", () => {
    expect(workspaceFromPathname("/admin/users/../../etc/leads", admin)).toEqual({ kind: "organization" });
    expect(workspaceFromPathname("/admin/users/not-a-uuid/leads", salesUser)).toEqual({ kind: "self" });
  });

  it("defaults to the own workspace while the viewer is still loading", () => {
    expect(workspaceFromPathname("/dashboard", null)).toEqual({ kind: "self" });
  });
});

describe("workspaceHref", () => {
  const user: Workspace = { kind: "user", userId: RAHUL };

  it("keeps navigation inside the selected user's workspace", () => {
    expect(workspaceHref(user, "dashboard")).toBe(`/admin/users/${RAHUL}/dashboard`);
    expect(workspaceHref(user, "activities")).toBe(`/admin/users/${RAHUL}/activities`);
  });

  it("uses top-level routes for own and organisation workspaces", () => {
    expect(workspaceHref({ kind: "self" }, "leads")).toBe("/leads");
    expect(workspaceHref({ kind: "organization" }, "leads")).toBe("/leads");
  });
});

describe("workspaceApiPath", () => {
  it.each<[Workspace, string]>([
    [{ kind: "self" }, "/api/v1/workspaces/me/leads"],
    [{ kind: "organization" }, "/api/v1/workspaces/all/leads"],
    [{ kind: "user", userId: RAHUL }, `/api/v1/workspaces/${RAHUL}/leads`],
  ])("scopes API calls to the workspace (%o)", (workspace, expected) => {
    expect(workspaceApiPath(workspace, "/leads")).toBe(expected);
  });
});

describe("activeSection", () => {
  it.each([
    ["/dashboard", "dashboard"],
    ["/leads/123", "leads"],
    [`/admin/users/${RAHUL}/pipeline`, "pipeline"],
    ["/admin/users", null],
    ["/settings", null],
  ])("%s -> %s", (pathname, expected) => {
    expect(activeSection(pathname)).toBe(expected);
  });
});

describe("lead links stay inside their workspace", () => {
  const id = "9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2c";
  const user = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b";

  it("own and organisation-wide leads live under /leads", () => {
    expect(leadHref({ kind: "self" }, id)).toBe(`/leads/${id}`);
    expect(leadHref({ kind: "organization" }, id, "edit")).toBe(`/leads/${id}/edit`);
    expect(newLeadHref({ kind: "self" })).toBe("/leads/new");
  });

  it("a selected user's leads live under their workspace", () => {
    expect(leadHref({ kind: "user", userId: user }, id)).toBe(`/admin/users/${user}/leads/${id}`);
    expect(newLeadHref({ kind: "user", userId: user })).toBe(`/admin/users/${user}/leads/new`);
  });

  it("the Leads section stays active on lead pages", () => {
    expect(activeSection(`/leads/${id}`)).toBe("leads");
    expect(activeSection(`/admin/users/${user}/leads/${id}/edit`)).toBe("leads");
  });
});
