/**
 * A team administrator in the CRM (ADR-0035). Their "all" workspace is their team: every
 * page names it as theirs ("Team overview", "My team"), never as the organisation's; it
 * shows owners and an owner filter like the organisation's (the API limits both to the
 * team), offers no shared-pipeline configuration (a Super admin's), and offers the team
 * counterparts of assigning, correcting and writing. A Super admin's wording is unchanged.
 * The UI only decides what to show: the API enforces every rule.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "@/components/shell/Sidebar";
import { activityPermissions } from "@/features/activities/hooks";
import { RequireCapability } from "@/features/auth/RequireCapability";
import { forgetActivityListState } from "@/features/activities/list-state";
import { forgetBoardState, pipelinePermissions } from "@/features/pipeline/hooks";
import { SearchLauncher } from "@/features/search/SearchLauncher";
import { AdminEntry } from "@/features/users/AdminEntry";
import { UserWorkspaceFrame } from "@/features/workspace/UserWorkspaceFrame";
import { ActivitiesView, AskWorkspaceView, DashboardView, OpportunityView, PipelineView } from "@/features/workspace/views";
import type { ActivityListItem, SearchResults } from "@/lib/api/types";
import type { Viewer } from "@/lib/viewer";
import type { Workspace } from "@/lib/workspace";
import { asListItem, makeActivity, page, SUMMARY } from "@/test/activity-fixtures";
import { makeDashboard } from "@/test/dashboard-fixtures";
import { adminViewer, LEAD_ID, makeViewer, OPPORTUNITY_OPTIONS_ROUTE, RAHUL_ID, salesViewer, teamAdminViewer } from "@/test/fixtures";
import { makeBoard, makeOpportunity, OPPORTUNITY_ID, PIPELINE_ROUTES, STAGES } from "@/test/pipeline-fixtures";
import { mockApi, type RecordedCall, renderWithProviders } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/dashboard", replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: vi.fn(), replace: nav.replace, back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() }),
}));

const ALL = "/api/v1/workspaces/all";
const tara = teamAdminViewer;
const ASSIGNEES = {
  "GET /api/v1/assignees": {
    status: 200,
    body: { results: [{ id: RAHUL_ID, full_name: "Rahul Sharma", email: "rahul@example.test" }], next: null, previous: null },
  },
};

beforeEach(() => {
  nav.replace.mockReset();
  forgetBoardState();
  forgetActivityListState();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the team's overview is named as the team's, never the organisation's", () => {
  it("Dashboard: Team overview, from the team's figures only, without the Super admin's panels", async () => {
    nav.pathname = "/dashboard";
    const api = mockApi({ [`GET ${ALL}/dashboard`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: tara });
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByText("Team overview")).toBeInTheDocument();
    expect(screen.queryByText("Organization overview")).not.toBeInTheDocument();
    expect(await screen.findByText("₹15,00,000")).toBeInTheDocument();
    // Whose each row is: the team has several owners.
    const meetings = screen.getByRole("region", { name: "Upcoming meetings" });
    expect(within(meetings).getByRole("listitem")).toHaveTextContent("Rahul Sharma");
    // Security events and privacy requests are organisation-wide (audit.view, privacy.manage).
    expect(screen.queryByRole("region", { name: "Security activity" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Data requests" })).not.toBeInTheDocument();
    expect(api.calls.map((c) => c.path)).toEqual([`${ALL}/dashboard`]);
  });

  it("Dashboard: a Super admin's is still the Organization overview", async () => {
    nav.pathname = "/dashboard";
    mockApi({ [`GET ${ALL}/dashboard`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: adminViewer });
    expect(screen.getByText("Organization overview")).toBeInTheDocument();
    expect(screen.queryByText("Team overview")).not.toBeInTheDocument();
  });

  it("Activities calendar: My calendar or My team (not Everyone)", async () => {
    vi.useFakeTimers({ now: new Date("2026-10-05T06:30:00Z"), toFake: ["Date"] });
    nav.pathname = "/activities";
    const task: ActivityListItem = asListItem(makeActivity({ due_at: "2026-10-05T12:30:00Z" }));
    const api = mockApi({
      [`GET ${ALL}/activity-summary`]: { status: 200, body: SUMMARY },
      [`GET ${ALL}/activities`]: (call: RecordedCall) => ({
        status: 200,
        body: page(call.query.get("page_size") === "100" && call.query.get("type") === "task" ? [task] : []),
      }),
    });
    renderWithProviders(<ActivitiesView />, { viewer: tara });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Calendar" }));
    const whose = screen.getByRole("group", { name: "Whose activities" });
    expect(within(whose).getAllByRole("button").map((b) => b.textContent)).toEqual(["My calendar", "My team"]);
    expect(within(whose).getByRole("button", { name: "My team" })).toHaveAttribute("aria-pressed", "true");
    await user.click(within(whose).getByRole("button", { name: "My calendar" }));
    await waitFor(() =>
      expect(api.calls.filter((c) => c.query.get("page_size") === "100").at(-1)!.query.get("owner")).toBe(tara.id),
    );
  });

  it("Activities list: each row names its owner, and the owner filter narrows the team's list", async () => {
    nav.pathname = "/activities";
    const api = mockApi({
      [`GET ${ALL}/activity-summary`]: { status: 200, body: SUMMARY },
      [`GET ${ALL}/activities`]: { status: 200, body: page([asListItem(makeActivity())]) },
      ...ASSIGNEES,
    });
    renderWithProviders(<ActivitiesView />, { viewer: tara });
    // Whose it is: the team has several owners (one's own list doesn't repeat the name).
    const table = await screen.findByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Owner" })).toBeInTheDocument();
    expect(within(table).getByRole("row", { name: /Send the revised quotation/ })).toHaveTextContent("Rahul Sharma");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^More filters/ }));
    const owner = screen.getByRole("combobox", { name: "Owner" });
    await waitFor(() => expect(within(owner).getAllByRole("option")[0]).toHaveTextContent("Anyone in my team"));
    await waitFor(() => expect(within(owner).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(owner, RAHUL_ID);
    // The list itself (not the calendar's 100-a-page reads) asks the API for that owner only.
    const listCalls = () => api.callsTo("GET", `${ALL}/activities`).filter((c) => c.query.get("page_size") !== "100");
    await waitFor(() => expect(listCalls().at(-1)!.query.get("owner")).toBe(RAHUL_ID));
  });

  it("Pipeline: owners shown and filtered within the team; no shared-pipeline configuration", async () => {
    nav.pathname = "/pipeline";
    const api = mockApi({ ...PIPELINE_ROUTES, [`GET ${ALL}/pipeline-board`]: { status: 200, body: makeBoard() }, ...ASSIGNEES });
    renderWithProviders(<PipelineView />, { viewer: tara });
    expect(await screen.findByText("Rahul Sharma")).toBeInTheDocument(); // the owner on the card
    const owner = screen.getByRole("combobox", { name: "Owner" });
    await waitFor(() => expect(within(owner).getAllByRole("option")[0]).toHaveTextContent("Everyone in my team"));
    await userEvent.setup().selectOptions(owner, RAHUL_ID);
    await waitFor(() => expect(api.callsTo("GET", `${ALL}/pipeline-board`).at(-1)!.query.get("owner")).toBe(RAHUL_ID));
    // Shared pipelines are a Super admin's (config.manage); new deals are the team's to add.
    expect(screen.queryByRole("button", { name: "New pipeline" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New opportunity" })).toBeInTheDocument();
  });

  it("Search: searching your team's records, each result naming its owner", async () => {
    nav.pathname = "/pipeline";
    const none = { results: [], has_more: false };
    const found: SearchResults = {
      query: "Asha",
      terms: ["Asha"],
      leads: {
        results: [
          {
            id: LEAD_ID,
            display_name: "Asha Mehta",
            organization_name: "Apollo Diagnostics",
            status: { key: "new", name: "New", category: "open" },
            owner: { id: RAHUL_ID, full_name: "Rahul Sharma", is_active: true },
          },
        ],
        has_more: false,
      },
      opportunities: none,
      tasks: none,
      meetings: none,
      notes: none,
    };
    const api = mockApi({ [`GET ${ALL}/search`]: { status: 200, body: found } });
    renderWithProviders(<SearchLauncher />, { viewer: tara });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /search/i }));
    expect(screen.getByText("Searching your team's records.")).toBeInTheDocument();
    expect(screen.queryByText("Searching all users' records.")).not.toBeInTheDocument();
    await user.type(screen.getByRole("combobox", { name: "Search leads, opportunities, tasks, meetings and notes" }), "Asha");
    expect(await screen.findByRole("option")).toHaveAccessibleName("Lead: Asha Mehta, Apollo Diagnostics, Rahul Sharma");
    expect(api.calls.every((c) => c.path === `${ALL}/search`)).toBe(true);
  });

  it("Ask Arkray: answers come only from your team's records", async () => {
    nav.pathname = "/ask";
    mockApi({
      [`GET ${ALL}/ask`]: { status: 200, body: { enabled: true, summaries: "available" } },
      [`GET ${ALL}/ask/conversations`]: { status: 200, body: [] },
    });
    renderWithProviders(<AskWorkspaceView />, { viewer: { ...tara, features: { ask: true } } });
    expect(await screen.findByText("Answers come only from your team's records.")).toBeInTheDocument();
  });
});

describe("what a team administrator may do in a workspace (mirrors the API's rules)", () => {
  const team: Workspace = { kind: "team" };
  const member: Workspace = { kind: "user", userId: RAHUL_ID };
  const own: Workspace = { kind: "self" };
  const organization: Workspace = { kind: "organization" };

  it("writes, assigns within the team and corrects closed deals in the team's overview, a member's and their own", () => {
    for (const workspace of [team, member, own]) {
      expect(pipelinePermissions(tara, workspace)).toMatchObject({ canWrite: true, canAssign: true, canCorrect: true });
      expect(activityPermissions(tara, workspace).canWrite).toBe(true);
    }
    // A new deal's owner is chosen in the overview only.
    expect(pipelinePermissions(tara, team).choosesOwner).toBe(true);
    expect(pipelinePermissions(tara, member).choosesOwner).toBe(false);
  });

  it("never the organisation's workspace", () => {
    expect(pipelinePermissions(tara, organization)).toMatchObject({ canWrite: false, canAssign: false, canCorrect: false });
    expect(activityPermissions(tara, organization).canWrite).toBe(false);
  });

  it("no correction during a support session (the API refuses it there)", () => {
    const session = { id: "s1", target: { id: RAHUL_ID, fullName: "Rahul Sharma" }, reason: "", startedAt: "", expiresAt: "" };
    expect(pipelinePermissions({ ...tara, supportSession: session }, member)).toMatchObject({ canWrite: true, canCorrect: false });
  });

  it("each team capability gates its own action", () => {
    const without = (capability: string): Viewer => ({ ...tara, capabilities: tara.capabilities.filter((c) => c !== capability) });
    expect(pipelinePermissions(without("crm.assign_team"), team)).toMatchObject({ canWrite: true, canAssign: false });
    expect(pipelinePermissions(without("crm.manage_team"), team)).toMatchObject({ canWrite: false, canAssign: false, canCorrect: false });
    expect(pipelinePermissions(without("crm.manage_team"), own)).toMatchObject({ canWrite: true, canCorrect: false });
  });

  it("a sales user gets none of it", () => {
    expect(pipelinePermissions(salesViewer, own)).toMatchObject({ canWrite: true, canAssign: false, canCorrect: false, choosesOwner: false });
    expect(pipelinePermissions(makeViewer(), member)).toMatchObject({ canWrite: false });
  });
});

describe("a team administrator on a deal of their team", () => {
  const DEAL = `${ALL}/opportunities/${OPPORTUNITY_ID}`;
  const EMPTY = { status: 200, body: { results: [], next: null, previous: null } };
  const routes = (opportunity: ReturnType<typeof makeOpportunity>) => ({
    ...PIPELINE_ROUTES,
    ...OPPORTUNITY_OPTIONS_ROUTE,
    ...ASSIGNEES,
    [`GET ${DEAL}`]: { status: 200, body: opportunity },
    [`GET ${DEAL}/history`]: EMPTY,
    [`GET ${DEAL}/negotiated-prices`]: EMPTY,
    [`GET ${DEAL}/corrections`]: EMPTY,
    [`GET ${DEAL}/timeline`]: EMPTY,
    [`GET ${ALL}/activities`]: EMPTY,
  });

  it("may change an open deal's owner (crm.assign_team)", async () => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
    mockApi(routes(makeOpportunity()));
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: tara });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "More actions for Hospital Analyzer Project" }));
    expect(screen.getByRole("menuitem", { name: "Change owner" })).toBeInTheDocument();
  });

  it("may correct a closed deal's figures (crm.manage_team)", async () => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
    const won = makeOpportunity({ status: "won", stage: STAGES.won, probability: "100.00", closed_at: "2026-09-30T04:30:00Z" });
    mockApi(routes(won));
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: tara });
    const deal = await screen.findByRole("region", { name: "Deal" });
    expect(within(deal).getByRole("button", { name: "Correct" })).toBeInTheDocument();
  });

  it("without crm.manage_team: no Correct and no owner change", async () => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
    const won = makeOpportunity({ status: "won", stage: STAGES.won, probability: "100.00", closed_at: "2026-09-30T04:30:00Z" });
    mockApi(routes(won));
    const viewOnly = { ...tara, capabilities: tara.capabilities.filter((c) => c !== "crm.manage_team") };
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: viewOnly });
    const deal = await screen.findByRole("region", { name: "Deal" });
    expect(within(deal).queryByRole("button", { name: "Correct" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "More actions for Hospital Analyzer Project" })).not.toBeInTheDocument();
  });
});

describe("the shell and page gates for a team administrator", () => {
  it("the Users page and a team member's workspace accept either capability of the pair", () => {
    const pageGate = ["users.manage", "users.manage_team"] as const;
    const { unmount } = renderWithProviders(<RequireCapability capability={pageGate}>My team page</RequireCapability>, { viewer: tara });
    expect(screen.getByText("My team page")).toBeInTheDocument();
    unmount();
    renderWithProviders(<RequireCapability capability={pageGate}>My team page</RequireCapability>, { viewer: salesViewer });
    expect(screen.queryByText("My team page")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("/admin leads a team administrator to their home, the team's dashboard", async () => {
    renderWithProviders(<AdminEntry />, { viewer: tara });
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/dashboard"));
  });

  it("a team member's workspace leads back to My team", async () => {
    nav.pathname = `/admin/users/${RAHUL_ID}/dashboard`;
    mockApi({
      [`GET /api/v1/workspaces/${RAHUL_ID}`]: {
        status: 200,
        body: { kind: "user", subject: { id: RAHUL_ID, full_name: "Rahul Sharma", status: "active", is_active: true } },
      },
    });
    renderWithProviders(<UserWorkspaceFrame userId={RAHUL_ID}>Rahul&apos;s dashboard</UserWorkspaceFrame>, { viewer: tara });
    const banner = await screen.findByRole("region", { name: "Workspace context" });
    expect(within(banner).getByRole("link", { name: "Back to My team" })).toHaveAttribute("href", "/admin/users");
    expect(screen.getByText("Rahul's dashboard")).toBeInTheDocument();
  });

  it("the sidebar offers the team's modules and My team", () => {
    nav.pathname = "/dashboard";
    mockApi({});
    renderWithProviders(<Sidebar />, { viewer: tara });
    expect(screen.getByRole("link", { name: "My team" })).toHaveAttribute("href", "/admin/users");
    expect(screen.queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Pipeline" })).toHaveAttribute("href", "/pipeline");
  });
});
