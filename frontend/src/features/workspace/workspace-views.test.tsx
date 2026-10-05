import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { adminViewer, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { asListItem, makeActivity, page, SUMMARY } from "@/test/activity-fixtures";
import { makeDashboard } from "@/test/dashboard-fixtures";
import { makeBoard, PIPELINE_ROUTES } from "@/test/pipeline-fixtures";
import { mockApi, renderWithProviders } from "@/test/render";

import { ActivitiesView, DashboardView, PipelineView } from "./views";

const navigation = vi.hoisted(() => ({ pathname: "/dashboard" }));
vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

beforeEach(() => {
  navigation.pathname = "/dashboard";
});

describe("Dashboard", () => {
  it("is the Admin Home for administrators: the organisation's figures only", async () => {
    const api = mockApi({ "GET /api/v1/workspaces/all/dashboard": { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: adminViewer });
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.getByText("Organization overview")).toBeInTheDocument();
    expect(await screen.findByText("₹15,00,000")).toBeInTheDocument();
    // CRM figures from the organisation only (plus the security log panel, not CRM data).
    expect(api.calls.map((c) => c.path).filter((p) => p !== "/api/v1/admin/security-events")).toEqual([
      "/api/v1/workspaces/all/dashboard",
    ]);
    expect(screen.getByRole("region", { name: "Security activity" })).toBeInTheDocument();
  });

  it("is a salesperson's own dashboard (no organisation or admin data is requested)", async () => {
    const api = mockApi({ "GET /api/v1/workspaces/me/dashboard": { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: salesViewer });
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(await screen.findByText("₹15,00,000")).toBeInTheDocument();
    expect(api.calls.map((c) => c.path)).toEqual(["/api/v1/workspaces/me/dashboard"]);
  });

  it("in another user's workspace, loads that user's dashboard only", async () => {
    navigation.pathname = `/admin/users/${RAHUL_ID}/dashboard`;
    const api = mockApi({ [`GET /api/v1/workspaces/${RAHUL_ID}/dashboard`]: { status: 200, body: makeDashboard() } });
    renderWithProviders(<DashboardView />, { viewer: adminViewer });
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(await screen.findByText("₹15,00,000")).toBeInTheDocument();
    expect(api.calls.every((c) => !c.path.includes("/workspaces/all") && !c.path.includes("/workspaces/me"))).toBe(true);
  });
});

describe("modules in another user's workspace", () => {
  it("the Activities (Phase 4) load that user's activities, scoped to their workspace", async () => {
    navigation.pathname = `/admin/users/${RAHUL_ID}/activities`;
    const api = mockApi({
      [`GET /api/v1/workspaces/${RAHUL_ID}/activities`]: { status: 200, body: page([asListItem(makeActivity())]) },
      [`GET /api/v1/workspaces/${RAHUL_ID}/activity-summary`]: { status: 200, body: SUMMARY },
    });
    renderWithProviders(<ActivitiesView />, { viewer: adminViewer });
    expect(screen.getByRole("heading", { name: "Activities" })).toBeInTheDocument();
    expect((await screen.findAllByText("Send the revised quotation")).length).toBeGreaterThan(0);
    expect(api.calls.every((c) => !c.path.includes("/workspaces/all") && !c.path.includes("/workspaces/me"))).toBe(true);
  });

  it("the Pipeline (Phase 3) loads that user's board, scoped to their workspace", async () => {
    navigation.pathname = `/admin/users/${RAHUL_ID}/pipeline`;
    const api = mockApi({
      ...PIPELINE_ROUTES,
      [`GET /api/v1/workspaces/${RAHUL_ID}/pipeline-board`]: { status: 200, body: makeBoard() },
    });
    renderWithProviders(<PipelineView />, { viewer: adminViewer });
    expect(await screen.findByText("Hospital Analyzer Project")).toBeInTheDocument();
    expect(api.callsTo("GET", `/api/v1/workspaces/${RAHUL_ID}/pipeline-board`)).toHaveLength(1);
    expect(api.calls.every((c) => !c.path.includes("/workspaces/all") && !c.path.includes("/workspaces/me"))).toBe(true);
  });
});
