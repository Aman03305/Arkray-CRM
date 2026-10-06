/**
 * Every page of a workspace, as a screen-reader user meets it (final audit UI-6 and the
 * remediation of its accessibility P3s): one h1 and no skipped heading level, every button
 * and link named, and a control repeated down a list naming its row.
 */
import { screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "@/components/shell/AppShell";
import { ambiguousControls, headingOutline, headingProblems, labelInNameProblems, unnamedControls } from "@/test/a11y";
import { adminViewer } from "@/test/fixtures";
import { renderWithProviders } from "@/test/render";
import { asListItem, page } from "@/test/activity-fixtures";
import { activitiesOf, type Person, RAHUL, workspaceWorld } from "@/test/workspace-world";

import { ActivitiesView, ActivityView, DashboardView, LeadView, OpportunityView, PipelineView } from "./views";

const nav = vi.hoisted(() => ({ pathname: "/" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), forward: vi.fn(), prefetch: vi.fn() }),
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

afterEach(() => {
  vi.unstubAllGlobals();
});

const PAGES: [string, (p: Person) => [string, ReactElement, string]][] = [
  ["Dashboard", (p) => ["dashboard", <DashboardView key="d" />, p.shown]],
  ["Pipeline", (p) => ["pipeline", <PipelineView key="p" />, `${p.mark}-OPPORTUNITY`]],
  ["Opportunity", (p) => [`pipeline/${p.opportunityId}`, <OpportunityView key="o" opportunityId={p.opportunityId} />, "Notes"]],
  ["Lead", (p) => [`leads/${p.leadId}`, <LeadView key="l" leadId={p.leadId} />, `${p.mark}-OPPORTUNITY`]],
  ["Activities", (p) => ["activities", <ActivitiesView key="a" />, `${p.mark}-TASK`]],
  ["Task", (p) => [`activities/${p.taskId}`, <ActivityView key="t" activityId={p.taskId} />, `${p.mark}-TASK`]],
  ["Meeting", (p) => [`activities/${p.meetingId}`, <ActivityView key="m" activityId={p.meetingId} />, `${p.mark}-MEETING`]],
  ["Note", (p) => [`activities/${p.noteId}`, <ActivityView key="n" activityId={p.noteId} />, `${p.mark}-NOTE`]],
];

describe.each(PAGES)("%s page, in the shell", (_name, at) => {
  it("one h1, no skipped level, every control named (for its row, on a list)", async () => {
    const world = workspaceWorld();
    // A deal's open work is its open tasks and meetings (the fake API ignores filters).
    const [task, meeting] = activitiesOf(RAHUL);
    world.fail((call) => call.rest === "/activities" && call.query.get("current") === "true", {
      status: 200,
      body: page([task!, meeting!].map((a) => asListItem(a))),
    });
    const [path, view, loaded] = at(RAHUL);
    // Rahul's workspace as an administrator opens it: the same views as his own pages, plus
    // the rail's workspace name (the fake API answers per user id).
    nav.pathname = `/admin/users/${RAHUL.id}/${path}`;
    renderWithProviders(<AppShell>{view}</AppShell>, { viewer: adminViewer });
    await screen.findAllByText(new RegExp(loaded.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    await waitFor(() => expect(document.querySelector('[aria-busy="true"]')).toBeNull());
    const main = screen.getByRole("main");
    expect({ outline: headingOutline(main), problems: headingProblems(main) }).toEqual({ outline: headingOutline(main), problems: [] });
    expect(unnamedControls(document.body)).toEqual([]);
    // Header included: the account button and the product link (axe, final audit UI-6a).
    expect(labelInNameProblems(document.body)).toEqual([]);
    // Lists have a table on wide screens and cards on phones; one at a time is on screen.
    expect(ambiguousControls(main, { exclude: ".md\\:hidden" })).toEqual([]);
    expect(ambiguousControls(main, { exclude: ".md\\:block" })).toEqual([]);
  });
});
