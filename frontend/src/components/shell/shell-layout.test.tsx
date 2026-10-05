import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AnchorHTMLAttributes, MouseEvent } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { forgetBoardState, useBoardSelection } from "@/features/pipeline/hooks";
import { adminViewer, RAHUL_ID, salesViewer } from "@/test/fixtures";
import { PIPELINE_ROUTES, PIPELINES, STAGES } from "@/test/pipeline-fixtures";
import { mockApi, renderWithProviders } from "@/test/render";

import { AppShell } from "./AppShell";
import { NavRail } from "./NavRail";
import { setPanelCollapsed } from "./panel-state";
import { Topbar } from "./Topbar";

const navigation = vi.hoisted(() => ({ pathname: "/dashboard" }));
vi.mock("next/navigation", () => ({
  usePathname: () => navigation.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));
// A plain anchor that behaves like next/link: onNavigate runs for a navigation in this tab
// (an unmodified primary click). Navigation itself is stopped (jsdom can't perform it).
vi.mock("next/link", () => ({
  default: ({
    href,
    onClick,
    onNavigate,
    children,
    ...rest
  }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; onNavigate?: (e: { preventDefault(): void }) => void }) => (
    <a
      href={href}
      onClick={(event: MouseEvent<HTMLAnchorElement>) => {
        onClick?.(event);
        const modified = event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0;
        if (!modified) onNavigate?.({ preventDefault: () => undefined });
        event.preventDefault();
      }}
      {...rest}
    >
      {children}
    </a>
  ),
}));

const CONFIG = PIPELINE_ROUTES;
const rahul = (status: "active" | "invited" | "deactivated") => ({
  [`GET /api/v1/workspaces/${RAHUL_ID}`]: {
    status: 200,
    body: { kind: "user", subject: { id: RAHUL_ID, full_name: "Rahul Sharma", status } },
  },
});

/** Shows what the board would display, as the pipelines panel leaves it. */
function BoardProbe() {
  const selection = useBoardSelection("me");
  return (
    <p data-testid="board">
      {selection.pipeline || "default"}|{selection.stage ?? "board"}
    </p>
  );
}

beforeEach(() => {
  navigation.pathname = "/dashboard";
  forgetBoardState();
  setPanelCollapsed(false);
  mockApi({ ...CONFIG });
});

describe("the navigation rail", () => {
  it("shows each module with its name and marks the current one", () => {
    navigation.pathname = "/leads";
    renderWithProviders(<NavRail />, { viewer: salesViewer });
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getAllByRole("link").map((l) => l.textContent)).toEqual([
      "Dashboard",
      "Pipeline",
      "Leads",
      "Activities",
      "Settings",
    ]);
    expect(within(nav).getByRole("link", { name: "Leads" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Dashboard" })).not.toHaveAttribute("aria-current");
    expect(within(nav).queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
  });

  it("keeps an administrator inside the selected user's workspace and names whose it is", async () => {
    mockApi(rahul("active"));
    navigation.pathname = `/admin/users/${RAHUL_ID}/pipeline`;
    renderWithProviders(<NavRail />, { viewer: adminViewer });
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("link", { name: "Leads" })).toHaveAttribute("href", `/admin/users/${RAHUL_ID}/leads`);
    expect(within(nav).getByRole("link", { name: "Pipeline" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Users" })).toHaveAttribute("href", "/admin/users");
    expect(await within(nav).findByRole("list", { name: "CRM for Rahul" })).toBeInTheDocument();
  });
});

describe("the header", () => {
  it("offers quick creation of the records the viewer may add, in this workspace", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Topbar onMenuClick={() => undefined} />, { viewer: salesViewer });
    const create = screen.getByRole("button", { name: "Create" });
    expect(create).toHaveAttribute("aria-expanded", "false");
    await user.click(create);
    expect(create).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "New lead" })).toHaveAttribute("href", "/leads/new");
    expect(screen.getByRole("link", { name: "New opportunity" })).toHaveAttribute("href", "/pipeline/new");

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("link", { name: "New lead" })).not.toBeInTheDocument();
    expect(create).toHaveFocus();
  });

  it("creates in the selected user's workspace, and not at all for a deactivated user", async () => {
    const user = userEvent.setup();
    mockApi(rahul("active"));
    navigation.pathname = `/admin/users/${RAHUL_ID}/leads`;
    const { unmount } = renderWithProviders(<Topbar onMenuClick={() => undefined} />, { viewer: adminViewer });
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(screen.getByRole("link", { name: "New lead" })).toHaveAttribute("href", `/admin/users/${RAHUL_ID}/leads/new`);
    unmount();

    mockApi(rahul("deactivated"));
    renderWithProviders(<Topbar onMenuClick={() => undefined} />, { viewer: adminViewer });
    await waitFor(() => expect(screen.queryByRole("button", { name: "Create" })).not.toBeInTheDocument());
  });

  it("has no Create button while the viewer is loading", () => {
    renderWithProviders(<Topbar onMenuClick={() => undefined} />, { viewer: null });
    expect(screen.queryByRole("button", { name: "Create" })).not.toBeInTheDocument();
    expect(screen.getByText("Loading user")).toBeInTheDocument();
  });

  it("opens the account menu with Settings and Sign out", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Topbar onMenuClick={() => undefined} />, { viewer: salesViewer });
    await user.click(screen.getByRole("button", { name: "Account: Priya Patel" }));
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    await user.click(document.body);
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });
});

describe("the pipelines panel", () => {
  it("appears on Pipeline pages only", async () => {
    const { unmount } = renderWithProviders(<AppShell>page</AppShell>, { viewer: salesViewer });
    expect(screen.queryByRole("region", { name: "Pipelines" })).not.toBeInTheDocument();
    unmount();

    navigation.pathname = "/pipeline";
    renderWithProviders(<AppShell>page</AppShell>, { viewer: salesViewer });
    const panel = screen.getByRole("region", { name: "Pipelines" });
    expect(await within(panel).findByRole("link", { name: "Sales Pipeline" })).toHaveAttribute("href", "/pipeline");
  });

  it("switches the board to a pipeline, or to one stage's full list", async () => {
    const user = userEvent.setup();
    navigation.pathname = "/pipeline";
    renderWithProviders(
      <AppShell>
        <BoardProbe />
      </AppShell>,
      { viewer: salesViewer },
    );
    const panel = screen.getByRole("region", { name: "Pipelines" });
    expect(screen.getByTestId("board")).toHaveTextContent("default|board");

    await user.click(await within(panel).findByRole("link", { name: STAGES.proposal.name }));
    expect(screen.getByTestId("board")).toHaveTextContent(`${PIPELINES.results[0]!.id}|${STAGES.proposal.id}`);
    expect(within(panel).getByRole("link", { name: STAGES.proposal.name })).toHaveAttribute("aria-current", "page");

    await user.click(within(panel).getByRole("link", { name: "Sales Pipeline" }));
    expect(screen.getByTestId("board")).toHaveTextContent(`${PIPELINES.results[0]!.id}|board`);
  });

  it("collapses to a strip and opens again", async () => {
    const user = userEvent.setup();
    navigation.pathname = "/pipeline";
    renderWithProviders(<AppShell>page</AppShell>, { viewer: salesViewer });
    await user.click(screen.getByRole("button", { name: "Hide pipelines" }));
    expect(screen.queryByRole("region", { name: "Pipelines" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show pipelines" }));
    expect(screen.getByRole("region", { name: "Pipelines" })).toBeInTheDocument();
  });
});
