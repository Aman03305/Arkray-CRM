import { describe, expect, it } from "vitest";

import { makeViewer, TEAM_ADMIN_CAPABILITIES } from "@/test/fixtures";

import type { Viewer } from "./viewer";
import {
  activeSection,
  canonicalUserPath,
  canWriteIn,
  isOverview,
  leadHref,
  newOpportunityHref,
  opportunityHref,
  SECTION_LABELS,
  workspaceApiPath,
  workspaceFromPathname,
  userIdFromPathname,
  userWorkspaceHref,
  workspaceHref,
  WORKSPACE_SECTIONS,
  type Workspace,
} from "./workspace";

const RAHUL = "3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b";
const salesUser: Viewer = makeViewer({ id: "u1", firstName: "Priya", lastName: "Patel", email: "p@x.test", capabilities: ["crm.access_own", "ai.query"] });
const admin: Viewer = makeViewer({ id: "a1", firstName: "Admin", lastName: "", email: "a@x.test", capabilities: ["crm.access_own", "crm.view_all", "workspace.view_any", "users.manage"] });

describe("workspaceFromPathname", () => {
  it("treats top-level module routes as the sales user's own workspace", () => {
    expect(workspaceFromPathname("/pipeline", salesUser)).toEqual({ kind: "self" });
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
      "/admin/users/../../etc/pipeline",
      "/admin/users/not-a-uuid/pipeline",
      `/admin/users/${RAHUL}x/pipeline`,
      `/admin/users/${RAHUL.replace("-", "%2F")}/pipeline`,
      `/admin/users/%25${RAHUL.slice(1)}/pipeline`, // double encoding is decoded once only
      "/admin/users/%E0%A4%A/pipeline", // malformed percent-encoding
      "/admin/users/me/pipeline",
      "/admin/users/all/pipeline",
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
      expect(workspaceFromPathname(`/admin/users/${spelling}/pipeline`, admin)).toEqual({ kind: "user", userId: RAHUL });
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
    expect(workspaceHref({ kind: "self" }, "pipeline")).toBe("/pipeline");
    expect(workspaceHref({ kind: "organization" }, "activities")).toBe("/activities");
  });
});

describe("the workspace sections", () => {
  it("are Dashboard, Pipeline and Activities: there is no Leads section (ADR-0027)", () => {
    expect(WORKSPACE_SECTIONS).toEqual(["dashboard", "pipeline", "activities"]);
    expect(WORKSPACE_SECTIONS).not.toContain("leads");
    expect(Object.values(SECTION_LABELS)).toEqual(["Dashboard", "Pipeline", "Activities"]);
  });
});

describe("workspaceApiPath", () => {
  it.each<[Workspace, string]>([
    [{ kind: "self" }, "/api/v1/workspaces/me/opportunities"],
    [{ kind: "organization" }, "/api/v1/workspaces/all/opportunities"],
    [{ kind: "user", userId: RAHUL }, `/api/v1/workspaces/${RAHUL}/opportunities`],
  ])("scopes API calls to the workspace (%o)", (workspace, expected) => {
    expect(workspaceApiPath(workspace, "/opportunities")).toBe(expected);
  });
});

describe("activeSection", () => {
  it.each([
    ["/dashboard", "dashboard"],
    ["/pipeline/123", "pipeline"],
    ["/activities", "activities"],
    [`/admin/users/${RAHUL}/pipeline`, "pipeline"],
    ["/admin/users", null],
    ["/settings", null],
  ])("%s -> %s", (pathname, expected) => {
    expect(activeSection(pathname)).toBe(expected);
  });

  it("knows no Leads section; a lead's page belongs to the Dashboard, in any workspace", () => {
    expect(activeSection("/leads")).toBeNull();
    expect(activeSection(`/admin/users/${RAHUL}/leads`)).toBeNull();
    expect(activeSection("/leads/9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2c")).toBe("dashboard");
    expect(activeSection(`/admin/users/${RAHUL}/leads/9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2c`)).toBe("dashboard");
  });
});

describe("lead links stay inside their workspace (ADR-0028)", () => {
  const id = "9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2c";

  it("own and organisation-wide leads live under /leads; a selected user's under theirs", () => {
    expect(leadHref({ kind: "self" }, id)).toBe(`/leads/${id}`);
    expect(leadHref({ kind: "organization" }, id)).toBe(`/leads/${id}`);
    expect(leadHref({ kind: "user", userId: RAHUL }, id)).toBe(`/admin/users/${RAHUL}/leads/${id}`);
  });

  it("an id is one path segment, never a way out of it", () => {
    expect(leadHref({ kind: "self" }, "../admin/users")).toBe("/leads/..%2Fadmin%2Fusers");
  });

  it("a lead's page belongs to its workspace, though Leads is not a module of it", () => {
    expect(WORKSPACE_SECTIONS).not.toContain("leads");
    expect(workspaceFromPathname(`/admin/users/${RAHUL}/leads/${id}`, makeViewer())).toEqual({ kind: "user", userId: RAHUL });
  });
});

describe("opportunity links stay inside their workspace", () => {
  const id = "9b1f7c2a-4d3e-4f5a-8b6c-7d8e9f0a1b2c";

  it("own and organisation-wide opportunities live under /pipeline", () => {
    expect(opportunityHref({ kind: "self" }, id)).toBe(`/pipeline/${id}`);
    expect(opportunityHref({ kind: "organization" }, id, "edit")).toBe(`/pipeline/${id}/edit`);
    expect(newOpportunityHref({ kind: "self" })).toBe("/pipeline/new");
    expect(newOpportunityHref({ kind: "organization" })).toBe("/pipeline/new");
  });

  it("a selected user's opportunities live under their workspace", () => {
    expect(opportunityHref({ kind: "user", userId: RAHUL }, id)).toBe(`/admin/users/${RAHUL}/pipeline/${id}`);
    expect(newOpportunityHref({ kind: "user", userId: RAHUL })).toBe(`/admin/users/${RAHUL}/pipeline/new`);
  });

  it("the new-opportunity form takes no lead: the deal carries its customer", () => {
    expect(newOpportunityHref.length).toBe(1);
    // @ts-expect-error -- a lead id is no longer accepted
    const withLead = newOpportunityHref({ kind: "self" }, id);
    expect(withLead).toBe("/pipeline/new");
    expect(withLead).not.toContain("lead");
  });
});

describe("canonical user-workspace URLs", () => {
  it("rewrites other spellings of the id to the one canonical URL, keeping the rest of the path", () => {
    expect(canonicalUserPath(`/admin/users/${RAHUL.toUpperCase()}/pipeline/new`)).toBe(`/admin/users/${RAHUL}/pipeline/new`);
    expect(canonicalUserPath(`/admin/users/${RAHUL.replace("-", "%2D")}`)).toBe(`/admin/users/${RAHUL}`);
  });

  it("leaves canonical, malformed and non-workspace paths alone", () => {
    expect(canonicalUserPath(`/admin/users/${RAHUL}/pipeline`)).toBeNull();
    expect(canonicalUserPath("/admin/users/not-a-uuid/pipeline")).toBeNull();
    expect(canonicalUserPath("/admin/users")).toBeNull();
    expect(canonicalUserPath("/pipeline")).toBeNull();
  });

  it("userIdFromPathname tells 'not a user path' (undefined) from 'not a user id' (null)", () => {
    expect(userIdFromPathname("/pipeline")).toBeUndefined();
    expect(userIdFromPathname("/admin/users/x/pipeline")).toBeNull();
    expect(userIdFromPathname(`/admin/users/${RAHUL}`)).toBe(RAHUL);
  });

  it("builds every selected-user link from the one route builder", () => {
    expect(userWorkspaceHref(RAHUL.toUpperCase())).toBe(`/admin/users/${RAHUL}/dashboard`);
    expect(userWorkspaceHref(RAHUL, "pipeline")).toBe(`/admin/users/${RAHUL}/pipeline`);
  });
});

describe("a team administrator's overview (ADR-0035)", () => {
  const teamAdmin: Viewer = makeViewer({ id: "t1", capabilities: [...TEAM_ADMIN_CAPABILITIES] });

  it("the top-level pages are their team's: the API's \"all\", named as the team", () => {
    expect(workspaceFromPathname("/pipeline", teamAdmin)).toEqual({ kind: "team" });
    expect(workspaceApiPath({ kind: "team" }, "opportunities")).toBe("/api/v1/workspaces/all/opportunities");
    expect(workspaceHref({ kind: "team" }, "activities")).toBe("/activities");
    expect(leadHref({ kind: "team" }, "l1")).toBe("/leads/l1");
  });

  it("a team member's workspace is a user workspace, like a super administrator's", () => {
    expect(workspaceFromPathname(`/admin/users/${RAHUL}/pipeline`, teamAdmin)).toEqual({ kind: "user", userId: RAHUL });
  });

  it("the overview is the organisation's or the team's; never one's own or a user's", () => {
    expect(isOverview({ kind: "organization" })).toBe(true);
    expect(isOverview({ kind: "team" })).toBe(true);
    expect(isOverview({ kind: "self" })).toBe(false);
    expect(isOverview({ kind: "user", userId: RAHUL })).toBe(false);
    expect(isOverview(null)).toBe(false);
  });
});

describe("canWriteIn (mirrors identity.workspaces.authorize_write)", () => {
  const teamAdmin: Viewer = makeViewer({ id: "t1", capabilities: [...TEAM_ADMIN_CAPABILITIES] });
  const superAdmin: Viewer = makeViewer({ id: "a1", capabilities: ["crm.access_own", "crm.view_all", "workspace.view_any", "crm.manage_any"] });
  const viewOnly: Viewer = makeViewer({ id: "v1", capabilities: ["crm.access_own", "crm.view_team", "workspace.view_team"] });
  const workspaces: Workspace[] = [{ kind: "self" }, { kind: "organization" }, { kind: "team" }, { kind: "user", userId: RAHUL }];

  it.each<[string, Viewer, boolean[]]>([
    ["a sales user", salesUser, [true, false, false, false]],
    ["a super administrator", superAdmin, [true, true, true, true]],
    // Never the organisation (a team administrator's overview is the team).
    ["a team administrator", teamAdmin, [true, false, true, true]],
    // Viewing the team never implies changing it.
    ["a viewer without crm.manage_team", viewOnly, [true, false, false, false]],
  ])("%s", (_who, viewer, expected) => {
    expect(workspaces.map((w) => canWriteIn(viewer, w))).toEqual(expected);
  });

  it("nothing while the viewer is unknown", () => {
    expect(workspaces.map((w) => canWriteIn(null, w))).toEqual([false, false, false, false]);
  });
});
