/**
 * Phase 6: the administrator's journey through a selected user's CRM
 * (docs/admin-user-workspace.md). Admin -> Users -> Rahul's name -> Rahul's Dashboard,
 * Pipeline and Activities (no Leads module, ADR-0027; a lead has a read-only page, ADR-0028),
 * without impersonation; and Rahul's records
 * never, not for one rendered frame, under Priya's banner (or the other way round).
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import UserWorkspaceSection from "@/app/(app)/admin/users/[userId]/[section]/page";
import { Sidebar } from "@/components/shell/Sidebar";
import { pipelineKeys } from "@/features/pipeline/api";
import { UsersPage } from "@/features/users/UsersPage";
import { UsersTable } from "@/features/users/UsersTable";
import { setFlash, useFlash } from "@/lib/flash";
import { adminViewer, makeAdminUser, makeViewer, salesViewer } from "@/test/fixtures";
import { createTestQueryClient, mockApi, renderWithProviders } from "@/test/render";
import { ACTOR, inWorkspace, markersOf, type Person, PRIYA, RAHUL, workspaceWorld } from "@/test/workspace-world";

import { workspaceKeys } from "./api";
import { SECTION_VIEWS } from "./section-views";
import { UserWorkspaceFrame } from "./UserWorkspaceFrame";
import {
  ActivitiesView,
  ActivityView,
  DashboardView,
  EditOpportunityView,
  LeadView,
  NewOpportunityView,
  OpportunityView,
  PipelineView,
} from "./views";

const nav = vi.hoisted(() => ({ pathname: "/", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() }),
  // As Next.js does: notFound() throws, and the route renders its not-found page.
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
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
describe("Admin Users: a user's CRM opens from an explicit link", () => {
  it("Open CRM leads to the user's Dashboard; the name opens their details; editing stays in the menu", async () => {
    const onAction = vi.fn();
    const onOpen = vi.fn();
    renderWithProviders(<UsersTable users={[makeAdminUser()]} loading={false} viewerId="a1" onAction={onAction} onOpen={onOpen} />, { viewer: adminViewer });
    const link = screen.getByRole("link", { name: "Open CRM for Rahul Sharma" });
    expect(link).toHaveAttribute("href", `${base(RAHUL)}/dashboard`);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Rahul Sharma" }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: RAHUL.id }));
    // Edit lives in the row's action menu, never on the name.
    await user.click(screen.getByRole("button", { name: "Actions for Rahul Sharma" }));
    await user.click(screen.getByRole("menuitem", { name: "Edit details" }));
    expect(onAction).toHaveBeenCalledWith("edit", expect.objectContaining({ id: RAHUL.id }));
  });

  it("a manager who may not open workspaces gets no Open CRM link (it would be a dead link)", () => {
    const manager = makeViewer({ id: "m1", capabilities: ["crm.access_own", "users.manage"] });
    renderWithProviders(<UsersTable users={[makeAdminUser()]} loading={false} viewerId="m1" onAction={vi.fn()} onOpen={vi.fn()} />, { viewer: manager });
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rahul Sharma" })).toBeInTheDocument();
  });
});

// --- every page of the workspace ----------------------------------------------------------------
const PAGES: [string, (p: Person) => [string, ReactElement], (p: Person) => string | null][] = [
  ["Dashboard", () => ["dashboard", <DashboardView key="d" />], (p) => p.shown],
  ["Pipeline", () => ["pipeline", <PipelineView key="p" />], (p) => `${p.mark}-OPPORTUNITY`],
  ["Opportunity", (p) => [`pipeline/${p.opportunityId}`, <OpportunityView key="o" opportunityId={p.opportunityId} />], (p) => `${p.mark}-OPPORTUNITY`],
  ["Edit opportunity", (p) => [`pipeline/${p.opportunityId}/edit`, <EditOpportunityView key="eo" opportunityId={p.opportunityId} />], () => null],
  ["New opportunity", () => [`pipeline/new`, <NewOpportunityView key="no" />], () => null],
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

// --- the workspace's modules (ADR-0027: no Leads module; ADR-0028: a lead's own page) ----------
describe("Rahul's workspace has no Leads module", () => {
  it("its navigation lists Dashboard, Pipeline and Activities, each in Rahul's workspace", async () => {
    workspaceWorld();
    openAt(`${base(RAHUL)}/dashboard`, RAHUL.id, <DashboardView />);
    const modules = await screen.findByRole("list", { name: "CRM for Rahul Sharma" });
    const links = within(modules).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual(["Dashboard", "Pipeline", "Activities"]);
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      `${base(RAHUL)}/dashboard`,
      `${base(RAHUL)}/pipeline`,
      `${base(RAHUL)}/activities`,
    ]);
    expect(within(modules).queryByRole("link", { name: /leads/i })).not.toBeInTheDocument();
    // His dashboard's new lead opens that lead's own page, in his workspace (ADR-0028).
    await screen.findAllByText(RAHUL.shown);
    expect([...document.querySelectorAll('a[href*="/leads"]')].map((link) => link.getAttribute("href"))).toEqual([
      `${base(RAHUL)}/leads/${RAHUL.leadId}`,
    ]);
    expect(linksLeavingWorkspace(RAHUL)).toEqual([]);
  });

  it("a lead's own page keeps the banner, shows Rahul's lead only, and every link stays in his workspace", async () => {
    const world = workspaceWorld();
    openAt(`${base(RAHUL)}/leads/${RAHUL.leadId}`, RAHUL.id, <LeadView leadId={RAHUL.leadId} />);
    await waitFor(() => expect(banner()).toHaveTextContent("Viewing CRM for: Rahul Sharma"));
    expect(await screen.findByRole("heading", { level: 1, name: "RAHUL-ONLY-LEAD" })).toBeInTheDocument();
    // Its opportunity opens in Rahul's workspace.
    expect(await screen.findByRole("link", { name: "RAHUL-ONLY-OPPORTUNITY" })).toHaveAttribute(
      "href",
      `${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`,
    );
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(world.calls.some((c) => c.path === `/api/v1/workspaces/${RAHUL.id}/leads/${RAHUL.leadId}`)).toBe(true);
    expect(world.foreignCalls(RAHUL.id)).toEqual([]);
    expect(world.leaks).toEqual([]);
    expect(screenHoldsNothingOf(PRIYA)).toEqual([]);
    expect(linksLeavingWorkspace(RAHUL)).toEqual([]);
  });

  it("Priya's lead is not found from Rahul's workspace", async () => {
    const world = workspaceWorld();
    openAt(`${base(RAHUL)}/leads/${PRIYA.leadId}`, RAHUL.id, <LeadView leadId={PRIYA.leadId} />);
    await waitFor(() => expect(banner()).toHaveTextContent("Viewing CRM for: Rahul Sharma"));
    await waitFor(() => expect(world.calls.some((c) => c.path === `/api/v1/workspaces/${RAHUL.id}/leads/${PRIYA.leadId}`)).toBe(true));
    expect(await screen.findByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
    expect(world.foreignCalls(RAHUL.id)).toEqual([]);
    expect(world.leaks).toEqual([]);
    expect(noRecordsOf(PRIYA)).toEqual([]);
    expect(linksLeavingWorkspace(RAHUL)).toEqual([]);
  });

  it("/admin/users/{id}/leads is not one of its sections: the route is not found", async () => {
    const open = (section: string) =>
      UserWorkspaceSection({ params: Promise.resolve({ userId: RAHUL.id, section }), searchParams: Promise.resolve({}) });
    await expect(open("leads")).rejects.toThrow("NEXT_NOT_FOUND");
    for (const section of ["dashboard", "pipeline", "activities"] as const) {
      expect((await open(section)).type).toBe(SECTION_VIEWS[section]);
    }
    expect(Object.keys(SECTION_VIEWS)).toEqual(["dashboard", "pipeline", "activities"]);
    // ...and no module of the sidebar claims that address as its page.
    workspaceWorld();
    nav.pathname = `${base(RAHUL)}/leads`;
    renderWithProviders(<Sidebar />, { viewer: adminViewer });
    for (const name of ["Dashboard", "Pipeline", "Activities"]) {
      expect(screen.getByRole("link", { name })).not.toHaveAttribute("aria-current");
    }
    expect(screen.queryByRole("link", { name: "Leads" })).not.toBeInTheDocument();
  });
});

// --- creating and editing stay in the workspace ---------------------------------------------------
describe("create and edit flows return to Rahul's workspace", () => {
  it("New opportunity -> save -> Rahul's opportunity; Edit -> save -> back on it", async () => {
    const world = workspaceWorld();
    const view = openAt(`${base(RAHUL)}/pipeline/new`, RAHUL.id, <NewOpportunityView />);
    const user = userEvent.setup();
    const create = await screen.findByRole("dialog", { name: "New opportunity" });
    // Rahul owns what is created in his workspace: no Owner (or Lead) to choose, and no name to
    // type (the server names it after its customer and instrument, ADR-0028).
    expect(within(create).queryByRole("combobox", { name: /owner|lead/i })).not.toBeInTheDocument();
    expect(within(create).queryByLabelText(/Opportunity name/)).not.toBeInTheDocument();
    await user.type(within(create).getByLabelText("Account name"), "City Lab");
    await user.type(within(create).getByLabelText("Customer name"), "Dr. Iyer");
    await user.type(within(create).getByLabelText("Contact (optional)"), "+91 98765 43210");
    await user.click(await within(create).findByRole("radio", { name: "Adams 8180 V" }));
    await user.type(within(create).getByLabelText("Installation price (₹)"), "111111");
    // The possible-duplicate check asks Rahul's workspace only.
    await waitFor(() => expect(world.calls.some((c) => c.rest === "/leads/duplicates")).toBe(true), { timeout: 3000 });
    expect(world.calls.filter((c) => c.rest === "/leads/duplicates").every((c) => c.workspace === RAHUL.id)).toBe(true);
    await user.click(within(create).getByRole("button", { name: "Create opportunity" }));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`));
    const post = world.calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe(`/api/v1/workspaces/${RAHUL.id}/opportunities`);
    // The workspace comes from the URL; the body never names an owner, creator, lead or workspace.
    expect(post.body).toMatchObject({ account_name: "City Lab", customer_name: "Dr. Iyer", contact_phone: "+91 98765 43210", instrument_name: "Adams 8180 V" });
    expect(Object.keys(post.body as object)).not.toEqual(expect.arrayContaining(["owner"]));
    expect(Object.keys(post.body as object)).not.toEqual(expect.arrayContaining(["lead"]));
    expect(post.body).not.toHaveProperty("title");
    expect(JSON.stringify(post.body)).not.toMatch(/created_by|workspace|user_id|actor/);

    view.go(`${base(RAHUL)}/pipeline/${RAHUL.opportunityId}/edit`, RAHUL.id, <EditOpportunityView opportunityId={RAHUL.opportunityId} />);
    const edit = await screen.findByRole("dialog", { name: "Edit opportunity" });
    // The panel names the opportunity it edits (its name is not editable).
    expect(edit).toHaveAccessibleDescription("RAHUL-ONLY-OPPORTUNITY");
    const customer = within(edit).getByLabelText("Customer name");
    await user.clear(customer);
    await user.type(customer, "Dr. Rao");
    await user.click(within(edit).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(nav.replace).toHaveBeenLastCalledWith(`${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`));
    const patch = world.calls.find((c) => c.method === "PATCH")!;
    expect(patch.path).toBe(`/api/v1/workspaces/${RAHUL.id}/opportunities/${RAHUL.opportunityId}`);
    expect(patch.body).toEqual({ version: 2, customer_name: "Dr. Rao" });
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
    // What it is about: one of Rahul's opportunities (never anyone else's).
    await within(dialog).findByRole("option", { name: "RAHUL-ONLY-OPPORTUNITY · RAHUL-ONLY-LEAD" });
    expect(within(dialog).queryByRole("option", { name: /PRIYA-ONLY/ })).not.toBeInTheDocument();
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Opportunity" }), RAHUL.opportunityId);
    await user.type(within(dialog).getByRole("textbox", { name: "Subject" }), "Call back");
    await user.click(within(dialog).getByRole("button", { name: "Create task" }));
    await waitFor(() => expect(world.calls.some((c) => c.method === "POST")).toBe(true));
    const post = world.calls.find((c) => c.method === "POST")!;
    expect(post.path).toBe(`/api/v1/workspaces/${RAHUL.id}/activities`);
    expect(post.body).toMatchObject({ type: "task", title: "Call back", opportunity: RAHUL.opportunityId });
    expect(post.body).not.toHaveProperty("lead");
    expect(JSON.stringify(post.body)).not.toMatch(/owner|created_by|workspace|user_id|actor/);
    expect(world.foreignCalls(RAHUL.id)).toEqual([]);
    expect(screenHoldsNothingOf(PRIYA)).toEqual([]);
  });

  it("a deactivated user's workspace stays readable, and the API's reason a new opportunity can't be added is shown", async () => {
    const reason = "This user's account isn't active, so nothing new can be added to their workspace.";
    const world = workspaceWorld([{ ...RAHUL, status: "deactivated" }, PRIYA]);
    world.fail((c) => c.method === "POST" && c.rest === "/opportunities", {
      status: 400,
      body: { error: { code: "invalid_input", message: "Check the details and try again.", details: { owner: [reason] }, request_id: "req-400" } },
    });
    openAt(`${base(RAHUL)}/pipeline/new`, RAHUL.id, <NewOpportunityView />);
    await waitFor(() => expect(banner()).toHaveTextContent("Status: Deactivated"));
    expect(banner()).toHaveTextContent("new records can't be added for them");
    expect((await screen.findAllByText("RAHUL-ONLY-OPPORTUNITY")).length).toBeGreaterThan(0); // still readable
    const create = screen.getByRole("dialog", { name: "New opportunity" });
    const user = userEvent.setup();
    await user.type(within(create).getByLabelText("Account name"), "City Lab");
    await user.type(within(create).getByLabelText("Customer name"), "Dr. Iyer");
    await user.type(within(create).getByLabelText("Installation price (₹)"), "1");
    await user.click(within(create).getByRole("button", { name: "Create opportunity" }));
    expect(await within(create).findByRole("alert")).toHaveTextContent(reason);
    expect(nav.replace).not.toHaveBeenCalled();
    expect(world.foreignCalls(RAHUL.id)).toEqual([]);
  });
});

// --- switching between users ----------------------------------------------------------------------
const MODULES: [string, (p: Person) => [string, ReactElement], (p: Person) => string][] = [
  ["Dashboard", () => ["dashboard", <DashboardView key="d" />], (p) => p.shown],
  ["Pipeline", () => ["pipeline", <PipelineView key="p" />], (p) => `${p.mark}-OPPORTUNITY`],
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
  it("Users -> Rahul Dashboard -> Rahul Opportunity -> Priya Dashboard -> Priya Activity, then Back and Forward: banner and data always match the URL", async () => {
    workspaceWorld();
    const view = openAt(`${base(RAHUL)}/dashboard`, RAHUL.id, <DashboardView />);
    const steps: [Person, string, ReactElement, string][] = [
      [RAHUL, "dashboard", <DashboardView key="1" />, RAHUL.shown],
      [RAHUL, `pipeline/${RAHUL.opportunityId}`, <OpportunityView key="2" opportunityId={RAHUL.opportunityId} />, "RAHUL-ONLY-OPPORTUNITY"],
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
  it("a bookmarked opportunity URL rebuilds Rahul's workspace from the URL alone (fresh page, empty cache)", async () => {
    const world = workspaceWorld();
    openAt(`${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`, RAHUL.id, <OpportunityView opportunityId={RAHUL.opportunityId} />, adminViewer, createTestQueryClient());
    expect(await screen.findByRole("heading", { level: 1, name: "RAHUL-ONLY-OPPORTUNITY" })).toBeInTheDocument();
    await waitFor(() => expect(banner()).toHaveTextContent("Viewing CRM for: Rahul Sharma"));
    expect(world.foreignCalls(RAHUL.id)).toEqual([]);
  });

  it("the selected user lives in the URL only: nothing is written to browser storage (tabs stay independent)", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    workspaceWorld();
    const view = openAt(`${base(RAHUL)}/pipeline`, RAHUL.id, <PipelineView />);
    await screen.findAllByText("RAHUL-ONLY-OPPORTUNITY");
    view.go(`${base(PRIYA)}/activities`, PRIYA.id, <ActivitiesView />);
    await screen.findAllByText("PRIYA-ONLY-TASK");
    const written = setItem.mock.calls.map((call) => call.join("="));
    expect(written.filter((w) => w.includes(RAHUL.id) || w.includes(PRIYA.id))).toEqual([]);
    setItem.mockRestore();
  });

  it("two tabs (two pages, two caches) show their own users side by side", async () => {
    workspaceWorld();
    const tab1 = openAt(`${base(RAHUL)}/pipeline`, RAHUL.id, <PipelineView />);
    expect(await screen.findAllByText("RAHUL-ONLY-OPPORTUNITY")).not.toHaveLength(0);
    tab1.unmount();
    openAt(`${base(PRIYA)}/pipeline`, PRIYA.id, <PipelineView />);
    expect(await screen.findAllByText("PRIYA-ONLY-OPPORTUNITY")).not.toHaveLength(0);
    expect(screenHoldsNothingOf(RAHUL)).toEqual([]);
  });
});

// --- URLs that don't name exactly one user -------------------------------------------------------
describe("URL canonicalisation (Phase 6 P1: banner of one workspace, data of another)", () => {
  it("a percent-encoded spelling of Rahul's id shows nothing until the canonical URL replaces it", async () => {
    const world = workspaceWorld();
    const encoded = `%${RAHUL.id.charCodeAt(0).toString(16)}${RAHUL.id.slice(1)}`;
    // Next.js decodes the layout's param; the pathname keeps the raw spelling.
    openAt(`/admin/users/${encoded}/pipeline`, RAHUL.id, <PipelineView />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`${base(RAHUL)}/pipeline`));
    expect(screen.queryByText("RAHUL-ONLY-OPPORTUNITY")).not.toBeInTheDocument();
    expect(world.leaks).toEqual([]); // before the fix: GET /api/v1/workspaces/all/...
    expect(world.calls.filter((c) => c.rest === "/pipeline-board")).toEqual([]);
  });

  it("an upper-case id is rewritten too (one address per workspace)", async () => {
    workspaceWorld();
    openAt(`/admin/users/${RAHUL.id.toUpperCase()}/pipeline/${RAHUL.opportunityId}`, RAHUL.id, <OpportunityView opportunityId={RAHUL.opportunityId} />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`));
  });

  it("a URL whose workspace isn't the layout's user is not found, and nothing is requested for it", async () => {
    const world = workspaceWorld();
    openAt(`${base(PRIYA)}/pipeline`, RAHUL.id, <PipelineView />);
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(world.calls.filter((c) => c.rest !== "")).toEqual([]);
    expect(noRecordsOf(PRIYA)).toEqual([]);
  });

  it("a malformed user id below /admin/users is not found: no fallback to the organisation", async () => {
    const world = workspaceWorld();
    nav.pathname = "/admin/users/not-a-user/pipeline";
    renderWithProviders(<PipelineView />, { viewer: adminViewer });
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(world.calls).toEqual([]);
  });
});

// --- the frame -----------------------------------------------------------------------------------
describe("the workspace frame", () => {
  it("never shows a name before the API has named the user, and names the actor separately", async () => {
    const world = workspaceWorld();
    const release = world.hold((c) => c.rest === "");
    openAt(`${base(RAHUL)}/activities`, RAHUL.id, <ActivitiesView />);
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
    openAt(`${base(RAHUL)}/activities`, RAHUL.id, <ActivitiesView />);
    // The module's own request may answer 404 first; the frame then replaces the whole page.
    await waitFor(() => expect(screen.queryByRole("region", { name: "Workspace context" })).not.toBeInTheDocument());
    expect(screen.getAllByRole("heading", { name: "Page not found" })).toHaveLength(1);
    expect(screen.queryByText(/RAHUL-ONLY/)).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/exists|permission|access to this user/i);
  });

  it("a failure to open the workspace fails closed (no module, no fallback), and can be retried", async () => {
    const world = workspaceWorld();
    world.fail((c) => c.workspace === RAHUL.id && c.rest === "", { status: 503, body: { error: { code: "service_unavailable", message: "Try again.", details: null, request_id: "req-503" } } });
    openAt(`${base(RAHUL)}/activities`, RAHUL.id, <ActivitiesView />);
    expect(await screen.findByText("This workspace couldn't be opened")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText("RAHUL-ONLY-TASK")).not.toBeInTheDocument();
    expect(world.leaks).toEqual([]);
  });

  it("a record of another workspace is 'not found', with the way back staying in this workspace", async () => {
    const world = workspaceWorld();
    openAt(`${base(RAHUL)}/pipeline/${PRIYA.opportunityId}`, RAHUL.id, <OpportunityView opportunityId={PRIYA.opportunityId} />);
    expect(await screen.findByRole("heading", { name: "Page not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Pipeline" })).toHaveAttribute("href", `${base(RAHUL)}/pipeline`);
    expect(screenHoldsNothingOf(PRIYA)).toEqual([]);
    expect(world.calls.every((c) => c.workspace === RAHUL.id)).toBe(true);
  });

  it("a manager without users.manage goes back to their Dashboard instead of a Users page they can't open", async () => {
    workspaceWorld();
    const viewerOnly = makeViewer({ id: "v1", fullName: "Vik Viewer", capabilities: ["crm.access_own", "workspace.view_any"] });
    openAt(`${base(RAHUL)}/pipeline`, RAHUL.id, <PipelineView />, viewerOnly);
    await waitFor(() => expect(banner()).toHaveTextContent("Rahul Sharma"));
    expect(within(banner()).getByRole("link", { name: "Back to Dashboard" })).toHaveAttribute("href", "/dashboard");
    // View-only: no create or edit actions are offered (the API refuses them anyway).
    expect((await screen.findAllByText("RAHUL-ONLY-OPPORTUNITY")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "New opportunity" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /New opportunity/ })).not.toBeInTheDocument();
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
    setFlash("RAHUL-ONLY-OPPORTUNITY created.", `${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`);
    nav.pathname = `${base(PRIYA)}/pipeline`; // Back pressed during the round trip: Priya's page mounts instead
    const priya = renderWithProviders(<Notice />, { viewer: adminViewer });
    expect(screen.getByText("no notice")).toBeInTheDocument();
    priya.unmount();
    // ...and it is gone: arriving at the original destination later shows nothing either.
    nav.pathname = `${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`;
    renderWithProviders(<Notice />, { viewer: adminViewer });
    expect(screen.getByText("no notice")).toBeInTheDocument();
  });

  it("the destination page shows it, once", () => {
    setFlash("Changes saved.", `${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`);
    nav.pathname = `${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`;
    const first = renderWithProviders(<Notice />, { viewer: adminViewer });
    expect(screen.getByText("Changes saved.")).toBeInTheDocument();
    first.unmount();
    renderWithProviders(<Notice />, { viewer: adminViewer });
    expect(screen.getByText("no notice")).toBeInTheDocument();
  });
});

// Was the lead reassignment review (P2); the owner now changes on the opportunity (ADR-0027).
describe("review: handing Rahul's opportunity to Priya from Rahul's workspace", () => {
  const rahul = { kind: "user", userId: RAHUL.id } as const;

  it("leaves Rahul's workspace for his Pipeline; nothing is cached as his fresh copy, and Priya's caches are untouched (P2)", async () => {
    const world = workspaceWorld();
    const client = createTestQueryClient();
    const view = openAt(`${base(PRIYA)}/pipeline`, PRIYA.id, <PipelineView />, adminViewer, client);
    await screen.findAllByText("PRIYA-ONLY-OPPORTUNITY");
    const priyaKeyed = () => client.getQueryCache().findAll().filter((q) => JSON.stringify(q.queryKey).includes(PRIYA.id));
    const priyaBefore = priyaKeyed().map((q) => JSON.stringify(q.state.data));

    view.go(`${base(RAHUL)}/pipeline/${RAHUL.opportunityId}`, RAHUL.id, <OpportunityView opportunityId={RAHUL.opportunityId} />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "More actions for RAHUL-ONLY-OPPORTUNITY" }));
    await user.click(screen.getByRole("menuitem", { name: "Change owner" }));
    const dialog = screen.getByRole("dialog", { name: "Change owner" });
    const owner = within(dialog).getByRole("combobox", { name: "New owner" });
    await within(owner).findByRole("option", { name: /^Priya Patel/ });
    await user.selectOptions(owner, PRIYA.id);
    await user.click(within(dialog).getByRole("button", { name: "Change owner" }));
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(`${base(RAHUL)}/pipeline`));
    const assign = world.calls.find((c) => c.method === "POST")!;
    expect(assign.path).toBe(`/api/v1/workspaces/${RAHUL.id}/opportunities/${RAHUL.opportunityId}/assign`);
    expect(assign.body).toEqual({ owner: PRIYA.id, version: 2 });

    // Rahul's copy of the deal is stale (it lives in Priya's workspace now), never refreshed as his.
    expect(client.getQueryState(pipelineKeys.detail(rahul, RAHUL.opportunityId))?.isInvalidated).toBe(true);
    for (const q of client.getQueryCache().findAll().filter((q) => JSON.stringify(q.queryKey).includes(RAHUL.id))) {
      expect(JSON.stringify(q.state.data ?? null)).not.toContain(PRIYA.id);
    }
    // ...and nothing was written into Priya's workspace from Rahul's.
    expect(priyaKeyed().map((q) => JSON.stringify(q.state.data))).toEqual(priyaBefore);

    // Rahul's Pipeline shows the notice and loads afresh.
    const boardsBefore = world.calls.filter((c) => c.workspace === RAHUL.id && c.rest === "/pipeline-board").length;
    view.go(`${base(RAHUL)}/pipeline`, RAHUL.id, <PipelineView />);
    expect(await screen.findByText("“RAHUL-ONLY-OPPORTUNITY” now belongs to Priya Patel.")).toBeInTheDocument();
    // Once its page is gone, Rahul's workspace keeps no copy of the deal at all.
    expect(client.getQueryState(pipelineKeys.detail(rahul, RAHUL.opportunityId))).toBeUndefined();
    await waitFor(() =>
      expect(world.calls.filter((c) => c.workspace === RAHUL.id && c.rest === "/pipeline-board").length).toBeGreaterThan(boardsBefore),
    );
    expect(world.leaks).toEqual([]);
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
    openAt(`${base(RAHUL)}/activities`, RAHUL.id, <ActivitiesView />);
    await screen.findByText("This workspace couldn't be opened");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("the deactivated user's New opportunity page has its one h1", async () => {
    workspaceWorld([{ ...RAHUL, status: "deactivated" }, PRIYA]);
    openAt(`${base(RAHUL)}/pipeline/new`, RAHUL.id, <NewOpportunityView />);
    await waitFor(() => expect(banner()).toHaveTextContent("Status: Deactivated"));
    await screen.findByRole("dialog", { name: "New opportunity" });
    await waitFor(() => expect(screen.getAllByRole("heading", { level: 1, hidden: true }).map((h) => h.textContent)).toEqual(["Pipeline"]));
  });

  it("screen readers hear whose CRM opened (titles don't name users)", async () => {
    workspaceWorld();
    const view = openAt(`${base(RAHUL)}/activities`, RAHUL.id, <ActivitiesView />);
    const region = () => document.querySelector('main p[aria-live="polite"]')!;
    await waitFor(() => expect(region()).toHaveTextContent("Viewing CRM for Rahul Sharma"));
    view.go(`${base(PRIYA)}/activities`, PRIYA.id, <ActivitiesView key="priya" />);
    await waitFor(() => expect(region()).toHaveTextContent("Viewing CRM for Priya Patel"));
  });

  it("an encoded section name still marks its own module, not Users, as the current page (P3)", () => {
    nav.pathname = `${base(RAHUL)}/%64ashboard`;
    renderWithProviders(<Sidebar />, { viewer: adminViewer });
    expect(screen.getByRole("link", { name: "Dashboard" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Users" })).not.toHaveAttribute("aria-current");
  });
});
