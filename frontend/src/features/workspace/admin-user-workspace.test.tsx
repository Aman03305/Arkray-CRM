/**
 * Phase 6: the administrator's journey through a selected user's CRM
 * (docs/admin-user-workspace.md). Admin -> Users -> Rahul's name -> Rahul's Dashboard,
 * Pipeline, Leads and Activities, without impersonation; and Rahul's records never, not for
 * one rendered frame, under Priya's banner (or the other way round).
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "@/components/shell/Sidebar";
import { leadKeys } from "@/features/leads/api";
import { syncAfterLeadWrite } from "@/features/leads/hooks";
import { UsersPage } from "@/features/users/UsersPage";
import { UsersTable } from "@/features/users/UsersTable";
import { setFlash, useFlash } from "@/lib/flash";
import { adminViewer, makeAdminUser, makeViewer, salesViewer } from "@/test/fixtures";
import { createTestQueryClient, mockApi, renderWithProviders } from "@/test/render";
import { ACTOR, inWorkspace, leadOf, markersOf, type Person, PRIYA, RAHUL, workspaceWorld } from "@/test/workspace-world";

import { workspaceKeys } from "./api";

import { UserWorkspaceFrame } from "./UserWorkspaceFrame";
import {
  ActivitiesView,
  ActivityView,
  DashboardView,
  EditLeadView,
  EditOpportunityView,
  LeadsView,
  LeadView,
  NewLeadView,
  NewOpportunityView,
  OpportunityView,
  PipelineView,
} from "./views";

const nav = vi.hoisted(() => ({ pathname: "/", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() }),
}));

beforeEach(() => {
  nav.push.mockReset();
  nav.replace.mockReset();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

const base = (p: Person) => `/admin/users/${p.id}`;

/** The layout of /admin/users/[userId]: the shell's sidebar, then the frame around the page. */
function Shell({ userId, children }: { userId: string; children: ReactNode }) {
  return (
    <>
      <Sidebar />
      <main>
        <UserWorkspaceFrame userId={userId}>{children}</UserWorkspaceFrame>
      </main>
    </>
  );
}

function openAt(path: string, userId: string, page: ReactElement, viewer = adminViewer, client = createTestQueryClient()) {
  nav.pathname = path;
  const view = renderWithProviders(<Shell userId={userId}>{page}</Shell>, { viewer, client });
  return {
    ...view,
    /** Client-side navigation: same providers, same cache, same mounted shell. */
    go(nextPath: string, nextUserId: string, nextPage: ReactElement) {
      nav.pathname = nextPath;
      view.rerender(<Shell userId={nextUserId}>{nextPage}</Shell>);
    },
  };
}

const banner = () => screen.getByRole("region", { name: "Workspace context" });

/**
 * Everything that reaches the screen from now on: text of every node inserted or changed,
 * and every link target. React commits synchronously into the DOM, so a single rendered
 * frame of the wrong data is caught here.
 */
function recordScreen() {
  const text: string[] = [];
  const hrefs: string[] = [];
  const take = (records: MutationRecord[]) => {
    for (const r of records) {
      if (r.type === "characterData") text.push(r.target.textContent ?? "");
      if (r.type === "attributes") hrefs.push((r.target as Element).getAttribute("href") ?? "");
      r.addedNodes.forEach((node) => {
        text.push(node.textContent ?? "");
        if (node instanceof Element) {
          if (node.matches("a[href]")) hrefs.push(node.getAttribute("href")!);
          node.querySelectorAll("a[href]").forEach((a) => hrefs.push(a.getAttribute("href")!));
        }
      });
    }
  };
  const observer = new MutationObserver(take);
  observer.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["href"] });
  return {
    /** Stop recording; true if anything of `person` ever reached the screen. */
    sawAnythingOf(person: Person): string[] {
      take(observer.takeRecords());
      observer.disconnect();
      const markers = markersOf(person);
      return [
        ...text.filter((t) => markers.some((m) => t.includes(m))),
        ...hrefs.filter((h) => h.includes(person.id) || markers.some((m) => h.includes(m))),
      ];
    },
  };
}

/** Nothing of `person` on screen right now (text or links). */
function screenHoldsNothingOf(person: Person) {
  const html = document.body.innerHTML;
  return markersOf(person).filter((m) => html.includes(m)).concat(html.includes(person.id) ? [person.id] : []);
}

/** None of `person`'s records on screen (their id may still be in navigation links). */
function noRecordsOf(person: Person) {
  const text = document.body.textContent ?? "";
  return [person.mark, person.shown].filter((m) => text.includes(m));
}

/** Every link on the page stays in this user's workspace (or is Users / Settings). */
function linksLeavingWorkspace(person: Person): string[] {
  return [...document.querySelectorAll("a[href]")]
    .map((a) => a.getAttribute("href")!)
    .filter((href) => href.startsWith("/"))
    .filter((href) => !href.startsWith(`${base(person)}/`) && href !== "/admin/users" && href !== "/settings");
}

// --- the entry point -------------------------------------------------------------------------
describe("Admin Users: a user's name opens their CRM", () => {
  it("the name links to the user's Dashboard with a name that says so; editing stays an explicit action", async () => {
    const onAction = vi.fn();
    renderWithProviders(<UsersTable users={[makeAdminUser()]} loading={false} viewerId="a1" onAction={onAction} />, { viewer: adminViewer });
    const link = screen.getByRole("link", { name: "Rahul Sharma, open CRM workspace" });
    expect(link).toHaveAttribute("href", `${base(RAHUL)}/dashboard`);
    expect(link).toHaveTextContent(/^Rahul Sharma$/);
    // Edit lives in the row's action menu, never on the name.
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Actions for Rahul Sharma" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    expect(onAction).toHaveBeenCalledWith("edit", expect.objectContaining({ id: RAHUL.id }));
  });

  it("a manager who may not open workspaces sees the name as text, not a dead link", () => {
    const manager = makeViewer({ id: "m1", capabilities: ["crm.access_own", "users.manage"] });
    renderWithProviders(<UsersTable users={[makeAdminUser()]} loading={false} viewerId="m1" onAction={vi.fn()} />, { viewer: manager });
    expect(screen.queryByRole("link", { name: /Rahul Sharma/ })).not.toBeInTheDocument();
    expect(screen.getByText("Rahul Sharma")).toBeInTheDocument();
  });
});

// --- every page of the workspace ----------------------------------------------------------------
const PAGES: [string, (p: Person) => [string, ReactElement], (p: Person) => string | null][] = [
  ["Dashboard", () => ["dashboard", <DashboardView key="d" />], (p) => p.shown],
  ["Pipeline", () => ["pipeline", <PipelineView key="p" />], (p) => `${p.mark}-OPPORTUNITY`],
  ["Opportunity", (p) => [`pipeline/${p.opportunityId}`, <OpportunityView key="o" opportunityId={p.opportunityId} />], (p) => `${p.mark}-OPPORTUNITY`],
  ["Edit opportunity", (p) => [`pipeline/${p.opportunityId}/edit`, <EditOpportunityView key="eo" opportunityId={p.opportunityId} />], () => null],
  ["New opportunity", (p) => [`pipeline/new`, <NewOpportunityView key="no" leadId={p.leadId} />], () => null],
  ["Leads", () => ["leads", <LeadsView key="l" />], (p) => `${p.mark}-LEAD`],
  ["Lead", (p) => [`leads/${p.leadId}`, <LeadView key="ld" leadId={p.leadId} />], (p) => `${p.mark}-LEAD`],
  ["Edit lead", (p) => [`leads/${p.leadId}/edit`, <EditLeadView key="el" leadId={p.leadId} />], () => null],
  ["New lead", () => ["leads/new", <NewLeadView key="nl" />], () => null],
  ["Activities", () => ["activities", <ActivitiesView key="a" />], (p) => `${p.mark}-TASK`],
  ["Task", (p) => [`activities/${p.taskId}`, <ActivityView key="t" activityId={p.taskId} />], (p) => `${p.mark}-TASK`],
  ["Meeting", (p) => [`activities/${p.meetingId}`, <ActivityView key="m" activityId={p.meetingId} />], (p) => `${p.mark}-MEETING`],
  ["Note", (p) => [`activities/${p.noteId}`, <ActivityView key="n" activityId={p.noteId} />], (p) => `${p.mark}-NOTE`],
];

describe.each(PAGES)("Rahul's %s, opened by the administrator", (_name, at, marker) => {
  it("keeps the banner, loads Rahul's records only, and every link stays in Rahul's workspace", async () => {
    const world = workspaceWorld();
    const [path, page] = at(RAHUL);
    openAt(`${base(RAHUL)}/${path}`, RAHUL.id, page);
    await waitFor(() => expect(banner()).toHaveTextContent("Viewing CRM for: Rahul Sharma"));
    expect(banner()).toHaveTextContent("Signed in as Anita Admin");
    const own = marker(RAHUL);
    if (own) expect((await screen.findAllByText(new RegExp(own.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")))).length).toBeGreaterThan(0);
    else await waitFor(() => expect(document.querySelector("form")).not.toBeNull());
    await waitFor(() => expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1)); // one h1, the page's
    expect(world.foreignCalls(RAHUL.id)).toEqual([]);
    expect(world.leaks).toEqual([]);
    expect(screenHoldsNothingOf(PRIYA)).toEqual([]);
    expect(linksLeavingWorkspace(RAHUL)).toEqual([]);
    // The sidebar says whose CRM its modules open, and marks one current page.
    expect(await screen.findByRole("list", { name: "CRM for Rahul Sharma" })).toBeInTheDocument();
    expect(document.querySelectorAll('[aria-current="page"]')).toHaveLength(1);
  });
});

// --- creating and editing stay in the workspace ---------------------------------------------------
describe("create and edit flows return to Rahul's workspace", () => {
  it("New lead -> save -> Rahul's new lead (created there, by the API's rules)", async () => {
    const world = workspaceWorld();
    openAt(`${base(RAHUL)}/leads/new`, RAHUL.id, <NewLeadView />);
    const user = userEvent.setup();
    await screen.findByRole("option", { name: "Referral" });
    expect(screen.getByText(/Owner:/)).toHaveTextContent("Owner: Rahul Sharma");
    await user.type(screen.getByLabelText("First name"), "Asha");
    await user.click(screen.getByRole("button", { name: "Create lead" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`${base(RAHUL)}/leads/${RAHUL.leadId.slice(0, -1)}9`));
    const post = world.calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe(`/api/v1/workspaces/${RAHUL.id}/leads`);
    // The workspace comes from the URL; the body never names an owner, creator or workspace.
    expect(Object.keys(post.body as object)).not.toEqual(expect.arrayContaining(["owner"]));
    expect(JSON.stringify(post.body)).not.toMatch(/created_by|workspace|user_id|actor/);
  });

  it("Edit lead -> save -> back on Rahul's lead", async () => {
    const world = workspaceWorld();
    openAt(`${base(RAHUL)}/leads/${RAHUL.leadId}/edit`, RAHUL.id, <EditLeadView leadId={RAHUL.leadId} />);
    const user = userEvent.setup();
    const city = await screen.findByLabelText(/^City/);
    await user.clear(city);
    await user.type(city, "Pune");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`${base(RAHUL)}/leads/${RAHUL.leadId}`));
    expect(world.calls.find((c) => c.method === "PATCH")!.path).toBe(`/api/v1/workspaces/${RAHUL.id}/leads/${RAHUL.leadId}`);
    expect(world.foreignCalls(RAHUL.id)).toEqual([]);
  });

  it("New opportunity -> save -> Rahul's opportunity; Edit -> save -> back on it", async () => {
    const world = workspaceWorld();
    const view = openAt(`${base(RAHUL)}/pipeline/new`, RAHUL.id, <NewOpportunityView leadId={RAHUL.leadId} />);
    const user = userEvent.setup();
    expect(await screen.findByRole("combobox", { name: "Lead" })).toHaveValue(RAHUL.leadId);
    await user.type(screen.getByLabelText("Title"), "Lab upgrade");
    await user.type(screen.getByLabelText("Value (₹)"), "111111");
    await user.click(screen.getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(`${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`));
    expect(world.calls.find((c) => c.method === "POST")!.path).toBe(`/api/v1/workspaces/${RAHUL.id}/opportunities`);

    view.go(`${base(RAHUL)}/pipeline/${RAHUL.opportunityId}/edit`, RAHUL.id, <EditOpportunityView opportunityId={RAHUL.opportunityId} />);
    const title = await screen.findByLabelText("Title");
    await user.clear(title);
    await user.type(title, "Lab upgrade, phase 2");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(nav.push).toHaveBeenLastCalledWith(`${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`));
    expect(world.foreignCalls(RAHUL.id)).toEqual([]);
    expect(world.leaks).toEqual([]);
  });

  it("New task from Rahul's Activities is created in Rahul's workspace, and the list stays his", async () => {
    const world = workspaceWorld();
    openAt(`${base(RAHUL)}/activities`, RAHUL.id, <ActivitiesView />);
    const user = userEvent.setup();
    await screen.findAllByText("RAHUL-ONLY-TASK");
    await user.click(screen.getByRole("button", { name: "New task" }));
    const dialog = screen.getByRole("dialog", { name: "New task" });
    await within(dialog).findByRole("option", { name: /RAHUL-ONLY-LEAD/ });
    expect(within(dialog).queryByRole("option", { name: /PRIYA-ONLY/ })).not.toBeInTheDocument();
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Lead" }), RAHUL.leadId);
    await user.type(within(dialog).getByRole("textbox", { name: "Subject" }), "Call back");
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    await waitFor(() => expect(world.calls.some((c) => c.method === "POST")).toBe(true));
    const post = world.calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe(`/api/v1/workspaces/${RAHUL.id}/activities`);
    expect(JSON.stringify(post.body)).not.toMatch(/owner|created_by|workspace|user_id|actor/);
    expect(world.foreignCalls(RAHUL.id)).toEqual([]);
    expect(screenHoldsNothingOf(PRIYA)).toEqual([]);
  });

  it("a deactivated user's workspace stays readable but offers no lead form that can't be saved", async () => {
    const world = workspaceWorld([{ ...RAHUL, status: "deactivated" }, PRIYA]);
    openAt(`${base(RAHUL)}/leads/new`, RAHUL.id, <NewLeadView />);
    expect(await screen.findByText("New leads can't be added for this user")).toBeInTheDocument();
    expect(screen.getByText(/Rahul Sharma's account is deactivated/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Leads" })).toHaveAttribute("href", `${base(RAHUL)}/leads`);
    expect(screen.queryByRole("button", { name: "Create lead" })).not.toBeInTheDocument();
    expect(banner()).toHaveTextContent("Status: Deactivated");
    expect(world.calls.some((c) => c.method !== "GET")).toBe(false);
  });
});

// --- switching between users ----------------------------------------------------------------------
const MODULES: [string, (p: Person) => [string, ReactElement], (p: Person) => string][] = [
  ["Dashboard", () => ["dashboard", <DashboardView key="d" />], (p) => p.shown],
  ["Pipeline", () => ["pipeline", <PipelineView key="p" />], (p) => `${p.mark}-OPPORTUNITY`],
  ["Leads", () => ["leads", <LeadsView key="l" />], (p) => `${p.mark}-LEAD`],
  ["Activities", () => ["activities", <ActivitiesView key="a" />], (p) => `${p.mark}-TASK`],
];

describe.each(MODULES)("%s: Rahul -> Priya", (_name, at, marker) => {
  it("Priya's screen never shows Rahul's records, not even while hers load (cache isolation)", async () => {
    workspaceWorld();
    const [section, page] = at(RAHUL);
    const view = openAt(`${base(RAHUL)}/${section}`, RAHUL.id, page);
    expect((await screen.findAllByText(marker(RAHUL), { exact: false })).length).toBeGreaterThan(0);
    await waitFor(() => expect(banner()).toHaveTextContent("Rahul Sharma"));

    const screenLog = recordScreen();
    view.go(`${base(PRIYA)}/${section}`, PRIYA.id, at(PRIYA)[1]);
    expect(screenHoldsNothingOf(RAHUL)).toEqual([]); // the very first frame
    expect((await screen.findAllByText(marker(PRIYA), { exact: false })).length).toBeGreaterThan(0);
    await waitFor(() => expect(banner()).toHaveTextContent("Viewing CRM for: Priya Patel"));
    expect(screenLog.sawAnythingOf(RAHUL)).toEqual([]);
  });

  it("a slow answer for Rahul that arrives after Priya's is never shown under Priya's banner", async () => {
    const world = workspaceWorld();
    const releaseRahul = world.hold(inWorkspace(RAHUL));
    const [section, page] = at(RAHUL);
    const view = openAt(`${base(RAHUL)}/${section}`, RAHUL.id, page);
    await waitFor(() => expect(world.calls.some((c) => c.workspace === RAHUL.id)).toBe(true));

    const screenLog = recordScreen();
    view.go(`${base(PRIYA)}/${section}`, PRIYA.id, at(PRIYA)[1]);
    expect((await screen.findAllByText(marker(PRIYA), { exact: false })).length).toBeGreaterThan(0);
    await act(async () => {
      releaseRahul();
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
    await waitFor(() => expect(banner()).toHaveTextContent("Viewing CRM for: Priya Patel"));
    expect(screenLog.sawAnythingOf(RAHUL)).toEqual([]);
    expect(world.leaks).toEqual([]);
  });

  it("rapid Rahul -> Priya -> Rahul with answers in reverse order ends on Rahul's data only", async () => {
    const world = workspaceWorld();
    const releaseRahul = world.hold(inWorkspace(RAHUL));
    const releasePriya = world.hold(inWorkspace(PRIYA));
    const [section, page] = at(RAHUL);
    const view = openAt(`${base(RAHUL)}/${section}`, RAHUL.id, page);
    view.go(`${base(PRIYA)}/${section}`, PRIYA.id, at(PRIYA)[1]);
    await waitFor(() => expect(world.calls.some((c) => c.workspace === PRIYA.id)).toBe(true));
    view.go(`${base(RAHUL)}/${section}`, RAHUL.id, at(RAHUL)[1]);

    const screenLog = recordScreen();
    await act(async () => {
      releasePriya();
      await new Promise((resolve) => setTimeout(resolve, 20));
      releaseRahul();
    });
    expect((await screen.findAllByText(marker(RAHUL), { exact: false })).length).toBeGreaterThan(0);
    await waitFor(() => expect(banner()).toHaveTextContent("Viewing CRM for: Rahul Sharma"));
    expect(screenLog.sawAnythingOf(PRIYA)).toEqual([]);
  });

  it("when Priya's request fails, the failure is shown: never Rahul's data, never the organisation's", async () => {
    const world = workspaceWorld();
    world.fail((c) => c.workspace === PRIYA.id && c.rest !== "", { status: 500, body: { error: { code: "server_error", message: "Boom.", details: null, request_id: "req-500" } } });
    const [section, page] = at(RAHUL);
    const view = openAt(`${base(RAHUL)}/${section}`, RAHUL.id, page);
    expect((await screen.findAllByText(marker(RAHUL), { exact: false })).length).toBeGreaterThan(0);

    const screenLog = recordScreen();
    view.go(`${base(PRIYA)}/${section}`, PRIYA.id, at(PRIYA)[1]);
    expect(await screen.findByText(/Something went wrong on our side/)).toBeInTheDocument();
    expect(screenLog.sawAnythingOf(RAHUL)).toEqual([]);
    expect(world.leaks).toEqual([]); // no fallback to /workspaces/all or /me
    expect(screenHoldsNothingOf(RAHUL)).toEqual([]);
  });
});

describe("Back and Forward", () => {
  it("Users -> Rahul Dashboard -> Rahul Lead -> Priya Dashboard -> Priya Activity, then Back and Forward: banner and data always match the URL", async () => {
    workspaceWorld();
    const view = openAt(`${base(RAHUL)}/dashboard`, RAHUL.id, <DashboardView />);
    const steps: [Person, string, ReactElement, string][] = [
      [RAHUL, "dashboard", <DashboardView key="1" />, RAHUL.shown],
      [RAHUL, `leads/${RAHUL.leadId}`, <LeadView key="2" leadId={RAHUL.leadId} />, "RAHUL-ONLY-LEAD"],
      [PRIYA, "dashboard", <DashboardView key="3" />, PRIYA.shown],
      [PRIYA, `activities/${PRIYA.taskId}`, <ActivityView key="4" activityId={PRIYA.taskId} />, "PRIYA-ONLY-TASK"],
    ];
    const visit = async ([person, path, page, marker]: (typeof steps)[number]) => {
      const other = person === RAHUL ? PRIYA : RAHUL;
      const screenLog = recordScreen();
      view.go(`${base(person)}/${path}`, person.id, page);
      expect((await screen.findAllByText(marker, { exact: false })).length).toBeGreaterThan(0);
      await waitFor(() => expect(banner()).toHaveTextContent(`Viewing CRM for: ${person.name}`));
      expect(screenLog.sawAnythingOf(other)).toEqual([]);
      expect(linksLeavingWorkspace(person)).toEqual([]);
    };
    for (const step of steps) await visit(step); // forward through the journey
    for (const step of [...steps].reverse().slice(1)) await visit(step); // Back, Back, Back
    for (const step of steps.slice(1)) await visit(step); // Forward, Forward, Forward
  });
});

describe("refresh, deep links and tabs", () => {
  it("a bookmarked lead URL rebuilds Rahul's workspace from the URL alone (fresh page, empty cache)", async () => {
    const world = workspaceWorld();
    openAt(`${base(RAHUL)}/leads/${RAHUL.leadId}`, RAHUL.id, <LeadView leadId={RAHUL.leadId} />, adminViewer, createTestQueryClient());
    expect(await screen.findByRole("heading", { level: 1, name: "RAHUL-ONLY-LEAD" })).toBeInTheDocument();
    await waitFor(() => expect(banner()).toHaveTextContent("Viewing CRM for: Rahul Sharma"));
    expect(world.foreignCalls(RAHUL.id)).toEqual([]);
  });

  it("the selected user lives in the URL only: nothing is written to browser storage (tabs stay independent)", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    workspaceWorld();
    const view = openAt(`${base(RAHUL)}/pipeline`, RAHUL.id, <PipelineView />);
    await screen.findAllByText("RAHUL-ONLY-OPPORTUNITY");
    view.go(`${base(PRIYA)}/leads`, PRIYA.id, <LeadsView />);
    await screen.findAllByText("PRIYA-ONLY-LEAD");
    const written = setItem.mock.calls.map((call) => call.join("="));
    expect(written.filter((w) => w.includes(RAHUL.id) || w.includes(PRIYA.id))).toEqual([]);
    setItem.mockRestore();
  });

  it("two tabs (two pages, two caches) show their own users side by side", async () => {
    workspaceWorld();
    const tab1 = openAt(`${base(RAHUL)}/leads`, RAHUL.id, <LeadsView />);
    expect(await screen.findAllByText("RAHUL-ONLY-LEAD")).not.toHaveLength(0);
    tab1.unmount();
    openAt(`${base(PRIYA)}/leads`, PRIYA.id, <LeadsView />);
    expect(await screen.findAllByText("PRIYA-ONLY-LEAD")).not.toHaveLength(0);
    expect(screenHoldsNothingOf(RAHUL)).toEqual([]);
  });
});

// --- URLs that don't name exactly one user -------------------------------------------------------
describe("URL canonicalisation (Phase 6 P1: banner of one workspace, data of another)", () => {
  it("a percent-encoded spelling of Rahul's id shows nothing until the canonical URL replaces it", async () => {
    const world = workspaceWorld();
    const encoded = `%${RAHUL.id.charCodeAt(0).toString(16)}${RAHUL.id.slice(1)}`;
    // Next.js decodes the layout's param; the pathname keeps the raw spelling.
    openAt(`/admin/users/${encoded}/leads`, RAHUL.id, <LeadsView />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`${base(RAHUL)}/leads`));
    expect(screen.queryByText("RAHUL-ONLY-LEAD")).not.toBeInTheDocument();
    expect(world.leaks).toEqual([]); // before the fix: GET /api/v1/workspaces/all/leads
    expect(world.calls.filter((c) => c.rest === "/leads")).toEqual([]);
  });

  it("an upper-case id is rewritten too (one address per workspace)", async () => {
    workspaceWorld();
    openAt(`/admin/users/${RAHUL.id.toUpperCase()}/pipeline/${RAHUL.opportunityId}`, RAHUL.id, <OpportunityView opportunityId={RAHUL.opportunityId} />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`));
  });

  it("a URL whose workspace isn't the layout's user is not found, and nothing is requested for it", async () => {
    const world = workspaceWorld();
    openAt(`${base(PRIYA)}/leads`, RAHUL.id, <LeadsView />);
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(world.calls.filter((c) => c.rest !== "")).toEqual([]);
    expect(noRecordsOf(PRIYA)).toEqual([]);
  });

  it("a malformed user id below /admin/users is not found: no fallback to the organisation", async () => {
    const world = workspaceWorld();
    nav.pathname = "/admin/users/not-a-user/leads";
    renderWithProviders(<LeadsView />, { viewer: adminViewer });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(world.calls).toEqual([]);
  });
});

// --- the frame -----------------------------------------------------------------------------------
describe("the workspace frame", () => {
  it("never shows a name before the API has named the user, and names the actor separately", async () => {
    const world = workspaceWorld();
    const release = world.hold((c) => c.rest === "");
    openAt(`${base(RAHUL)}/leads`, RAHUL.id, <LeadsView />);
    expect(screen.getByText("Loading user name")).toBeInTheDocument();
    expect(banner()).not.toHaveTextContent("Rahul Sharma");
    act(() => release());
    await waitFor(() => expect(banner()).toHaveTextContent("Viewing CRM for: Rahul Sharma"));
    expect(banner()).toHaveTextContent("Signed in as Anita Admin");
    // One request describes the workspace, shared by the banner, the sidebar and the forms.
    expect(world.calls.filter((c) => c.rest === "")).toHaveLength(1);
  });

  it("an unknown or forbidden user is a plain 404 (never 'exists but not yours'), with no module data", async () => {
    const world = workspaceWorld();
    world.fail(inWorkspace(RAHUL), { status: 404, body: { error: { code: "not_found", message: "Not found.", details: null, request_id: "r" } } });
    openAt(`${base(RAHUL)}/leads`, RAHUL.id, <LeadsView />);
    // The module's own request may answer 404 first; the frame then replaces the whole page.
    await waitFor(() => expect(screen.queryByRole("region", { name: "Workspace context" })).not.toBeInTheDocument());
    expect(screen.getAllByRole("heading", { name: "Page not found" })).toHaveLength(1);
    expect(screen.queryByText(/RAHUL-ONLY/)).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/exists|permission|access to this user/i);
  });

  it("a failure to open the workspace fails closed (no module, no fallback), and can be retried", async () => {
    const world = workspaceWorld();
    world.fail((c) => c.workspace === RAHUL.id && c.rest === "", { status: 503, body: { error: { code: "service_unavailable", message: "Try again.", details: null, request_id: "req-503" } } });
    openAt(`${base(RAHUL)}/leads`, RAHUL.id, <LeadsView />);
    expect(await screen.findByText("This workspace couldn't be opened")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText("RAHUL-ONLY-LEAD")).not.toBeInTheDocument();
    expect(world.leaks).toEqual([]);
  });

  it("a record of another workspace is 'not found', with the way back staying in this workspace", async () => {
    const world = workspaceWorld();
    openAt(`${base(RAHUL)}/leads/${PRIYA.leadId}`, RAHUL.id, <LeadView leadId={PRIYA.leadId} />);
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Leads" })).toHaveAttribute("href", `${base(RAHUL)}/leads`);
    expect(screenHoldsNothingOf(PRIYA)).toEqual([]);
    expect(world.calls.every((c) => c.workspace === RAHUL.id)).toBe(true);
  });

  it("a manager without users.manage goes back to their Dashboard instead of a Users page they can't open", async () => {
    workspaceWorld();
    const viewerOnly = makeViewer({ id: "v1", fullName: "Vik Viewer", capabilities: ["crm.access_own", "workspace.view_any"] });
    openAt(`${base(RAHUL)}/leads`, RAHUL.id, <LeadsView />, viewerOnly);
    await waitFor(() => expect(banner()).toHaveTextContent("Rahul Sharma"));
    expect(within(banner()).getByRole("link", { name: "Back to Dashboard" })).toHaveAttribute("href", "/dashboard");
    // View-only: no create or edit actions are offered (the API refuses them anyway).
    expect(screen.queryByRole("link", { name: /New lead/ })).not.toBeInTheDocument();
  });

  it("a sales user typing another user's workspace URL gets 'not found' from the API, and nothing else", async () => {
    const world = workspaceWorld();
    world.fail(() => true, { status: 404, body: { error: { code: "not_found", message: "Not found.", details: null, request_id: "r" } } });
    openAt(`${base(PRIYA)}/dashboard`, PRIYA.id, <DashboardView />, salesViewer);
    await waitFor(() => expect(screen.queryByRole("region", { name: "Workspace context" })).not.toBeInTheDocument());
    expect(screen.getAllByRole("heading", { name: "Page not found" })).toHaveLength(1);
    expect(noRecordsOf(PRIYA)).toEqual([]);
    expect(world.leaks).toEqual([]);
  });
});

describe("mutations stay in their workspace's cache", () => {
  it("completing Rahul's task updates Rahul's caches only; Priya's cached records are untouched", async () => {
    const world = workspaceWorld();
    const client = createTestQueryClient();
    const view = openAt(`${base(PRIYA)}/activities`, PRIYA.id, <ActivitiesView />, adminViewer, client);
    await screen.findAllByText("PRIYA-ONLY-TASK");
    const priyaBefore = client.getQueryCache().findAll().filter((q) => JSON.stringify(q.queryKey).includes(PRIYA.id)).map((q) => JSON.stringify(q.state.data));

    view.go(`${base(RAHUL)}/activities/${RAHUL.taskId}`, RAHUL.id, <ActivityView activityId={RAHUL.taskId} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^Complete/ }));
    await waitFor(() => expect(world.calls.some((c) => c.method === "POST" && c.rest.endsWith("/complete"))).toBe(true));
    await waitFor(() => expect(screen.getAllByText(/Completed/).length).toBeGreaterThan(0));

    const queries = client.getQueryCache().findAll();
    // No cache entry of Priya's workspace holds anything of Rahul's...
    for (const q of queries.filter((q) => JSON.stringify(q.queryKey).includes(PRIYA.id))) {
      expect(JSON.stringify(q.state.data ?? null)).not.toMatch(/RAHUL-ONLY/);
    }
    // ...and Rahul's writes never wrote into Priya's (an invalidation marks them stale; it
    // never puts data there).
    const priyaAfter = queries.filter((q) => JSON.stringify(q.queryKey).includes(PRIYA.id)).map((q) => JSON.stringify(q.state.data));
    expect(priyaAfter).toEqual(priyaBefore);
    // Every workspace-sensitive key carries its workspace.
    for (const q of queries) {
      const key = JSON.stringify(q.queryKey);
      if (/RAHUL-ONLY/.test(JSON.stringify(q.state.data ?? null))) expect(key).toContain(RAHUL.id);
    }
    expect(world.leaks).toEqual([]);
    expect(ACTOR.full_name).toBe("Anita Admin"); // the fake API records the admin as actor
  });
});

// --- regressions from the Phase 6 independent review ---------------------------------------------
describe("review: a notice belongs to the page it was sent to", () => {
  function Notice() {
    const [notice] = useFlash();
    return <p>{notice ?? "no notice"}</p>;
  }

  it("a navigation that never landed leaves nothing of Rahul's for Priya's page (P1 under the brief's rule)", () => {
    setFlash("RAHUL-ONLY-LEAD was converted. This is the new opportunity.", `${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`);
    nav.pathname = `${base(PRIYA)}/leads`; // Back pressed during the round trip: Priya's page mounts instead
    const priya = renderWithProviders(<Notice />, { viewer: adminViewer });
    expect(screen.getByText("no notice")).toBeInTheDocument();
    priya.unmount();
    // ...and it is gone: arriving at the original destination later shows nothing either.
    nav.pathname = `${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`;
    renderWithProviders(<Notice />, { viewer: adminViewer });
    expect(screen.getByText("no notice")).toBeInTheDocument();
  });

  it("the destination page shows it, once", () => {
    setFlash("Lead created.", `${base(RAHUL)}/leads/${RAHUL.leadId}`);
    nav.pathname = `${base(RAHUL)}/leads/${RAHUL.leadId}`;
    const first = renderWithProviders(<Notice />, { viewer: adminViewer });
    expect(screen.getByText("Lead created.")).toBeInTheDocument();
    first.unmount();
    renderWithProviders(<Notice />, { viewer: adminViewer });
    expect(screen.getByText("no notice")).toBeInTheDocument();
  });
});

describe("review: a reassignment that finishes after its page was left", () => {
  const rahul = { kind: "user", userId: RAHUL.id } as const;
  const priya = { kind: "user", userId: PRIYA.id } as const;

  it("is not cached as the old workspace's fresh copy of a lead that now lives elsewhere (P2)", () => {
    const client = createTestQueryClient();
    client.setQueryData(leadKeys.detail(priya, PRIYA.leadId), leadOf(PRIYA)); // left on screen earlier, no observer now
    syncAfterLeadWrite(client, priya, leadOf(PRIYA, { owner: { id: RAHUL.id, full_name: RAHUL.name, is_active: true } }));
    expect(client.getQueryData(leadKeys.detail(priya, PRIYA.leadId))).toBeUndefined();
  });

  it("while its page still shows it, the page keeps its copy until it leaves (unchanged)", () => {
    const client = createTestQueryClient();
    const shown = client.getQueryCache().build(client, { queryKey: leadKeys.detail(rahul, RAHUL.leadId) });
    vi.spyOn(shown, "getObserversCount").mockReturnValue(1); // the lead page, still mounted
    syncAfterLeadWrite(client, rahul, leadOf(RAHUL, { owner: { id: PRIYA.id, full_name: PRIYA.name, is_active: true } }));
    expect(client.getQueryData(leadKeys.detail(rahul, RAHUL.leadId))).toMatchObject({ owner: { id: PRIYA.id } });
  });
});

describe("review: user management refreshes the workspace banner", () => {
  it("deactivating Rahul marks his cached workspace description stale, so his banner and forms don't say Active (P3)", async () => {
    const client = createTestQueryClient();
    client.setQueryData(workspaceKeys.subject(RAHUL.id), { id: RAHUL.id, full_name: RAHUL.name, status: "active" });
    const rahul = makeAdminUser();
    mockApi({
      "GET /api/v1/admin/users": { status: 200, body: { results: [rahul], next: null, previous: null } },
      [`POST /api/v1/admin/users/${rahul.id}/deactivate`]: { status: 200, body: { ...rahul, status: "deactivated", status_label: "Deactivated" } },
    });
    nav.pathname = "/admin/users";
    renderWithProviders(<UsersPage />, { viewer: adminViewer, client });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Actions for Rahul Sharma" }));
    await user.click(screen.getByRole("menuitem", { name: "Deactivate" }));
    await user.click(within(screen.getByRole("alertdialog", { name: "Deactivate Rahul Sharma?" })).getByRole("button", { name: "Deactivate" }));
    await screen.findByText("Rahul Sharma was deactivated.");
    expect(client.getQueryState(workspaceKeys.subject(RAHUL.id))?.isInvalidated).toBe(true);
  });
});

describe("review: accessibility of the workspace's own states", () => {
  it("a workspace that can't be opened still has its one h1", async () => {
    const world = workspaceWorld();
    world.fail((c) => c.workspace === RAHUL.id && c.rest === "", { status: 503, body: { error: { code: "service_unavailable", message: "Try again.", details: null, request_id: "r" } } });
    openAt(`${base(RAHUL)}/leads`, RAHUL.id, <LeadsView />);
    await screen.findByText("This workspace couldn't be opened");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("the deactivated user's New lead page has its h1", async () => {
    workspaceWorld([{ ...RAHUL, status: "deactivated" }, PRIYA]);
    openAt(`${base(RAHUL)}/leads/new`, RAHUL.id, <NewLeadView />);
    await screen.findByText("New leads can't be added for this user");
    expect(screen.getByRole("heading", { level: 1, name: "New lead" })).toBeInTheDocument();
  });

  it("screen readers hear whose CRM opened (titles don't name users)", async () => {
    workspaceWorld();
    const view = openAt(`${base(RAHUL)}/leads`, RAHUL.id, <LeadsView />);
    const region = () => document.querySelector('main p[aria-live="polite"]')!;
    await waitFor(() => expect(region()).toHaveTextContent("Viewing CRM for Rahul Sharma"));
    view.go(`${base(PRIYA)}/leads`, PRIYA.id, <LeadsView key="priya" />);
    await waitFor(() => expect(region()).toHaveTextContent("Viewing CRM for Priya Patel"));
  });

  it("an encoded section name still marks its own module, not Users, as the current page (P3)", () => {
    nav.pathname = `${base(RAHUL)}/%64ashboard`;
    renderWithProviders(<Sidebar />, { viewer: adminViewer });
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Users" })).not.toHaveAttribute("aria-current");
  });
});
