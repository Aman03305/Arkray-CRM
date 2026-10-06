/**
 * Regression tests for the final whole-software audit's frontend findings (UI-1 to UI-3, UI-5,
 * UI-7): one per confirmed defect, each asserting the corrected behaviour.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { forgetBoardState } from "@/features/pipeline/hooks";
import { PipelineView } from "@/features/workspace/views";
import { OPPORTUNITY_OPTIONS_ROUTE, salesViewer } from "@/test/fixtures";
import { makeBoard, makeOpportunity, PIPELINE_ROUTES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders } from "@/test/render";

const nav = vi.hoisted(() => ({ pathname: "/pipeline", push: vi.fn(), replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: nav.push, replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
}));

const BOARD = "/api/v1/workspaces/me/pipeline-board";
const CREATE = "/api/v1/workspaces/me/opportunities";
const scrolled = vi.fn();

beforeEach(() => {
  nav.pathname = "/pipeline";
  nav.push.mockReset();
  nav.replace.mockReset();
  forgetBoardState();
  scrolled.mockReset();
  // jsdom has no layout: record which element a panel asked to bring into view.
  Element.prototype.scrollIntoView = function (this: Element) {
    scrolled(this);
  };
});
afterEach(() => {
  delete (Element.prototype as Partial<Element>).scrollIntoView;
});

async function fillValidForm(panel: HTMLElement) {
  const user = userEvent.setup();
  await user.type(within(panel).getByLabelText("Account name"), "City Lab");
  await user.type(within(panel).getByLabelText("Customer name"), "Dr. Iyer");
  await user.type(within(panel).getByLabelText("Installation price (₹)"), "12,50,000");
  return user;
}

describe("the New opportunity panel", () => {
  it("UI-7: opens on its first field, not on the close button", async () => {
    mockApi({ ...PIPELINE_ROUTES, ...OPPORTUNITY_OPTIONS_ROUTE, [`GET ${BOARD}`]: { status: 200, body: makeBoard([]) } });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New opportunity" }));
    const panel = screen.getByRole("dialog", { name: "New opportunity" });
    await waitFor(() => expect(within(panel).getByLabelText("Account name")).toHaveFocus());
  });

  it("UI-1: a server error from a submit made at the bottom of a long form is brought into view", async () => {
    mockApi({
      ...PIPELINE_ROUTES,
      ...OPPORTUNITY_OPTIONS_ROUTE,
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([]) },
      [`POST ${CREATE}`]: apiError(500, "internal_error", "Something went wrong on our side."),
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user0 = userEvent.setup();
    await user0.click(await screen.findByRole("button", { name: "New opportunity" }));
    const panel = screen.getByRole("dialog", { name: "New opportunity" });
    const user = await fillValidForm(panel);
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    const alert = await within(panel).findByRole("alert");
    await waitFor(() => expect(scrolled).toHaveBeenCalledWith(alert));
  });

  it("UI-3: a 409 on create shows the server's message, not an endless 'Loading the latest version…'", async () => {
    mockApi({
      ...PIPELINE_ROUTES,
      ...OPPORTUNITY_OPTIONS_ROUTE,
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([]) },
      [`POST ${CREATE}`]: apiError(409, "conflict", "This opportunity was already created from another window."),
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user0 = userEvent.setup();
    await user0.click(await screen.findByRole("button", { name: "New opportunity" }));
    const panel = screen.getByRole("dialog", { name: "New opportunity" });
    const user = await fillValidForm(panel);
    await user.click(within(panel).getByRole("button", { name: "Create opportunity" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("already created from another window");
    expect(within(panel).queryByText("Loading the latest version…")).not.toBeInTheDocument();
  });

  it("UI-2: Discard returns focus to the button that opened the panel, not to the page heading", async () => {
    mockApi({
      ...PIPELINE_ROUTES,
      ...OPPORTUNITY_OPTIONS_ROUTE,
      [`GET ${BOARD}`]: { status: 200, body: makeBoard([]) },
      [`POST ${CREATE}`]: { status: 201, body: makeOpportunity() },
    });
    renderWithProviders(<PipelineView />, { viewer: salesViewer });
    const user = userEvent.setup();
    const opener = await screen.findByRole("button", { name: "New opportunity" });
    await user.click(opener);
    const panel = screen.getByRole("dialog", { name: "New opportunity" });
    await user.type(within(panel).getByLabelText("Customer name"), "Typed and abandoned");
    await user.click(within(panel).getByRole("button", { name: "Cancel" }));
    await user.click(within(screen.getByRole("alertdialog", { name: "Discard your changes?" })).getByRole("button", { name: "Discard" }));
    expect(screen.queryByRole("dialog", { name: "New opportunity" })).not.toBeInTheDocument();
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
