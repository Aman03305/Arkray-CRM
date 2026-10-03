import { describe, expect, it } from "vitest";

import { makeViewer } from "@/test/fixtures";

import type { Viewer } from "./viewer";
import {
  activeSection,
  canonicalUserPath,
  leadHref,
  newLeadHref,
  workspaceApiPath,
  workspaceFromPathname,
  userIdFromPathname,
  userWorkspaceHref,
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

  it("a malformed user id names no workspace: never the viewer's own or the organisation's", () => {
    for (const path of [
      "/admin/users/../../etc/leads",
      "/admin/users/not-a-uuid/leads",
      `/admin/users/${RAHUL}x/leads`,
      `/admin/users/${RAHUL.replace("-", "%2F")}/leads`,
      `/admin/users/%25${RAHUL.slice(1)}/leads`, // double encoding is decoded once only
      "/admin/users/%E0%A4%A/leads", // malformed percent-encoding
      "/admin/users/me/leads",
      "/admin/users/all/leads",
    ]) {
      expect(workspaceFromPathname(path, admin)).toBeNull();
      expect(workspaceFromPathname(path, salesUser)).toBeNull();
      expect(workspaceFromPathname(path, null)).toBeNull();
    }
  });

  it("decodes the user id once, exactly as the route's param is decoded (one user, one workspace)", () => {
    const spellings = [
      RAHUL,
      RAHUL.toUpperCase(),
      `%${RAHUL.charCodeAt(0).toString(16)}${RAHUL.slice(1)}`,
      RAHUL.replace("-", "%2D"),
      RAHUL.replace("-", "%2d"),
    ];
    for (const spelling of spellings) {
      expect(workspaceFromPathname(`/admin/users/${spelling}/leads`, admin)).toEqual({ kind: "user", userId: RAHUL });
    }
  });

  it("the Users page itself is not a workspace path", () => {
    expect(workspaceFromPathname("/admin/users", admin)).toEqual({ kind: "organization" });
    expect(workspaceFromPathname("/admin/users/", admin)).toEqual({ kind: "organization" });
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

describe("canonical user-workspace URLs", () => {
  it("rewrites other spellings of the id to the one canonical URL, keeping the rest of the path", () => {
    expect(canonicalUserPath(`/admin/users/${RAHUL.toUpperCase()}/leads/new`)).toBe(`/admin/users/${RAHUL}/leads/new`);
    expect(canonicalUserPath(`/admin/users/${RAHUL.replace("-", "%2D")}`)).toBe(`/admin/users/${RAHUL}`);
  });

  it("leaves canonical, malformed and non-workspace paths alone", () => {
    expect(canonicalUserPath(`/admin/users/${RAHUL}/leads`)).toBeNull();
    expect(canonicalUserPath("/admin/users/not-a-uuid/leads")).toBeNull();
    expect(canonicalUserPath("/admin/users")).toBeNull();
    expect(canonicalUserPath("/leads")).toBeNull();
  });

  it("userIdFromPathname tells 'not a user path' (undefined) from 'not a user id' (null)", () => {
    expect(userIdFromPathname("/leads")).toBeUndefined();
    expect(userIdFromPathname("/admin/users/x/leads")).toBeNull();
    expect(userIdFromPathname(`/admin/users/${RAHUL}`)).toBe(RAHUL);
  });

  it("builds every selected-user link from the one route builder", () => {
    expect(userWorkspaceHref(RAHUL.toUpperCase())).toBe(`/admin/users/${RAHUL}/dashboard`);
    expect(userWorkspaceHref(RAHUL, "pipeline")).toBe(`/admin/users/${RAHUL}/pipeline`);
  });
});
