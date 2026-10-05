import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { UserWorkspaceFrame } from "@/features/workspace/UserWorkspaceFrame";
import { useFlash } from "@/lib/flash";
import { VIEWER_QUERY_KEY } from "@/lib/query-client";
import type { SupportSession, Viewer } from "@/lib/viewer";
import { ViewerProvider } from "@/lib/viewer-context";
import { adminViewer, RAHUL_ID } from "@/test/fixtures";
import { apiError, createTestQueryClient, mockApi, renderWithProviders } from "@/test/render";

import { AppShell } from "./AppShell";
import { SupportSessionBar } from "./SupportSessionBar";

const nav = vi.hoisted(() => ({ pathname: "/admin/users/3f2b8c1e-9a4d-4e2f-8b7a-1c2d3e4f5a6b/dashboard", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
}));

const NOW = new Date("2026-10-05T10:05:00Z");
const SESSION: SupportSession = {
  id: "0a1b2c3d-0000-4000-8000-000000000001",
  target: { id: RAHUL_ID, fullName: "Rahul Sharma" },
  reason: "Fixing a lead",
  startedAt: "2026-10-05T10:00:00Z",
  expiresAt: "2026-10-05T10:30:00Z",
};
const supporting: Viewer = { ...adminViewer, fullName: "Anita Admin", supportSession: SESSION };
const inRahul = (section: string) => `/admin/users/${RAHUL_ID}/${section}`;
const WORKSPACE = {
  [`GET /api/v1/workspaces/${RAHUL_ID}`]: {
    status: 200,
    body: { kind: "user", subject: { id: RAHUL_ID, full_name: "Rahul Sharma", status: "active" } },
  },
};

beforeEach(() => {
  nav.pathname = inRahul("dashboard");
  nav.push.mockReset();
  nav.replace.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
});

const banner = () => screen.getByRole("region", { name: "Support session" });

describe("the support session banner", () => {
  it("names the supported user, who is signed in, and the minutes left, counting down each minute", () => {
    vi.useFakeTimers({ now: NOW, toFake: ["setTimeout", "clearTimeout", "Date"] });
    mockApi({});
    renderWithProviders(<SupportSessionBar />, { viewer: supporting });
    expect(banner()).toHaveTextContent("Support session · Rahul Sharma · 25 min left");
    expect(banner()).toHaveTextContent("Signed in as Anita Admin");
    expect(banner()).not.toHaveTextContent("Signed in as Rahul");
    act(() => vi.advanceTimersByTime(60_000));
    expect(banner()).toHaveTextContent("24 min left");
    act(() => vi.advanceTimersByTime(30_000));
    expect(banner()).toHaveTextContent("24 min left");
    act(() => vi.advanceTimersByTime(30_000));
    expect(banner()).toHaveTextContent("23 min left");
  });

  it("announces the start politely", async () => {
    mockApi({});
    renderWithProviders(<SupportSessionBar />, { viewer: supporting });
    const live = document.querySelector('[aria-live="polite"]')!;
    await waitFor(() => expect(live).toHaveTextContent("Support session started for Rahul Sharma"));
    expect(banner()).not.toContainElement(live as HTMLElement);
  });

  it("asks the API again when the time is up", () => {
    vi.useFakeTimers({ now: new Date("2026-10-05T10:29:30Z"), toFake: ["setTimeout", "clearTimeout", "Date"] });
    mockApi({ "GET /api/v1/auth/me": apiError(503, "service_unavailable", "Down.") });
    const client = createTestQueryClient();
    client.setQueryData(VIEWER_QUERY_KEY, supporting);
    renderWithProviders(<SupportSessionBar />, { viewer: supporting, client });
    expect(banner()).toHaveTextContent("1 min left");
    expect(client.getQueryState(VIEWER_QUERY_KEY)?.isInvalidated).toBe(false);
    act(() => vi.advanceTimersByTime(30_000));
    expect(banner()).toHaveTextContent("Ending…");
    expect(client.getQueryState(VIEWER_QUERY_KEY)?.isInvalidated).toBe(true);
  });

  it("Exit ends the session, forgets it, and goes back to Users", async () => {
    const api = mockApi({
      "DELETE /api/v1/admin/support-sessions/current": { status: 204 },
      "GET /api/v1/auth/me": apiError(503, "service_unavailable", "Down."),
    });
    const client = createTestQueryClient();
    client.setQueryData(VIEWER_QUERY_KEY, supporting);
    renderWithProviders(<SupportSessionBar />, { viewer: supporting, client });
    await userEvent.setup().click(within(banner()).getByRole("button", { name: "Exit" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/admin/users"));
    expect(api.callsTo("DELETE", "/api/v1/admin/support-sessions/current")).toHaveLength(1);
    expect(client.getQueryData<Viewer>(VIEWER_QUERY_KEY)?.supportSession).toBeNull();
  });

  it("says so if Exit fails, and stays", async () => {
    mockApi({ "DELETE /api/v1/admin/support-sessions/current": apiError(503, "service_unavailable", "Down.") });
    renderWithProviders(<SupportSessionBar />, { viewer: supporting });
    await userEvent.setup().click(within(banner()).getByRole("button", { name: "Exit" }));
    expect(await within(banner()).findByRole("alert")).toHaveTextContent("Couldn't exit the support session.");
    expect(nav.push).not.toHaveBeenCalled();
  });
});

function FlashProbe() {
  const [notice] = useFlash();
  return <p data-testid="flash">{notice}</p>;
}

describe("the shell during a support session", () => {
  it("replaces pages outside the supported user's workspace with theirs, without rendering them", async () => {
    mockApi(WORKSPACE);
    for (const [path, section] of [
      ["/leads", "leads"],
      ["/dashboard", "dashboard"],
      ["/admin/users", "dashboard"],
      ["/settings", "dashboard"],
      ["/admin/users/5c4b3a29-1807-4f6e-9d5c-4b3a29180716/pipeline", "pipeline"],
    ]) {
      nav.pathname = path!;
      nav.replace.mockReset();
      const { unmount } = renderWithProviders(<AppShell>Admin page</AppShell>, { viewer: supporting });
      expect(screen.queryByText("Admin page")).not.toBeInTheDocument();
      await waitFor(() => expect(nav.replace).toHaveBeenCalledWith(inRahul(section!)));
      unmount();
    }
  });

  it("shows the supported user's pages with one banner (not the workspace banner too), and no Users or Settings", async () => {
    mockApi(WORKSPACE);
    nav.pathname = inRahul("leads");
    renderWithProviders(
      <AppShell>
        <UserWorkspaceFrame userId={RAHUL_ID}>Rahul&apos;s leads</UserWorkspaceFrame>
      </AppShell>,
      { viewer: supporting },
    );
    expect(await screen.findByText("Rahul's leads")).toBeInTheDocument();
    expect(nav.replace).not.toHaveBeenCalled();
    expect(banner()).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Workspace context" })).not.toBeInTheDocument();
    const rail = screen.getAllByRole("navigation", { name: "Main" })[0]!;
    expect(within(rail).getByRole("link", { name: "Leads" })).toHaveAttribute("href", inRahul("leads"));
    expect(within(rail).queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    expect(within(rail).queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Arkray CRM" })).toHaveAttribute("href", inRahul("dashboard"));
  });

  it("without a support session there is no banner and nothing is redirected", () => {
    mockApi({});
    nav.pathname = "/dashboard";
    renderWithProviders(<AppShell>Admin page</AppShell>, { viewer: adminViewer });
    expect(screen.getByText("Admin page")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Support session" })).not.toBeInTheDocument();
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("when the session ends by itself, says so on Users", async () => {
    mockApi(WORKSPACE);
    const view = renderWithProviders(
      <ViewerProvider viewer={supporting}>
        <AppShell>page</AppShell>
      </ViewerProvider>,
    );
    expect(banner()).toBeInTheDocument();
    view.rerender(
      <ViewerProvider viewer={{ ...supporting, supportSession: null }}>
        <AppShell>page</AppShell>
      </ViewerProvider>,
    );
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/admin/users"));
    expect(screen.queryByRole("region", { name: "Support session" })).not.toBeInTheDocument();

    nav.pathname = "/admin/users";
    view.rerender(<FlashProbe />);
    expect(screen.getByTestId("flash")).toHaveTextContent("Support session ended");
  });
});
