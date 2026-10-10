/**
 * Privacy requests for administrators (privacy.manage): the Data requests page (your own
 * exports, polled while one is being prepared, downloaded by an ordinary link), and, from a
 * user's details, exporting their data or pseudonymising a deactivated user, confirmed by
 * typing their email.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UsersPage } from "@/features/users/UsersPage";
import type { DataExport } from "@/lib/api/types";
import { ambiguousControls, headingProblems, labelInNameProblems, unnamedControls } from "@/test/a11y";
import { adminViewer, makeAdminUser, makeViewer } from "@/test/fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { confirmsEmail } from "./PseudonymiseDialog";
import { DataRequestsPage, POLL_MS } from "./DataRequestsPage";

vi.mock("next/navigation", () => ({
  usePathname: () => "/admin/users",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const EXPORTS = "/api/v1/admin/privacy/exports";

function makeExport(overrides: Partial<DataExport> = {}): DataExport {
  return {
    id: "0b1c2d3e-0000-4000-8000-000000000001",
    subject_type: "lead",
    subject_id: "4d2c1b0a-9f8e-4d7c-8b6a-5f4e3d2c1b0a",
    reference: "DSR-2026-014",
    status: "ready",
    created_at: "2026-10-10T04:30:00Z",
    ready_at: "2026-10-10T04:31:00Z",
    expires_at: "2026-10-17T04:31:00Z",
    size: 48_640,
    sha256: "ab".repeat(32),
    files_included: 3,
    files_omitted: 1,
    error_code: "",
    downloads: 0,
    ...overrides,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("the Data requests page", () => {
  it("lists your exports, with a download link for a ready one", async () => {
    mockApi({
      [`GET ${EXPORTS}`]: {
        status: 200,
        body: {
          results: [
            makeExport(),
            makeExport({ id: "0b1c2d3e-0000-4000-8000-000000000002", reference: "HR-7", subject_type: "user", status: "expired", size: 1024, files_omitted: 0 }),
            makeExport({ id: "0b1c2d3e-0000-4000-8000-000000000003", reference: "DSR-9", status: "failed", size: null, ready_at: null, expires_at: null, error_code: "too_large" }),
          ],
        },
      },
    });
    renderWithProviders(<DataRequestsPage />, { viewer: adminViewer });
    expect(screen.getByRole("heading", { level: 1, name: "Data requests" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Your data exports" });
    const [, ready, expired, failed] = within(table).getAllByRole("row");
    expect(ready).toHaveTextContent("DSR-2026-014");
    expect(ready).toHaveTextContent("Customer");
    expect(ready).toHaveTextContent("Ready");
    expect(ready).toHaveTextContent("48 KB");
    expect(ready).toHaveTextContent("3 included");
    expect(ready).toHaveTextContent("1 omitted");
    // An ordinary link (same-origin, the session cookie authenticates it): never fetched here.
    const download = within(ready!).getByRole("link", { name: "Download DSR-2026-014" });
    expect(download).toHaveAttribute("href", `${EXPORTS}/0b1c2d3e-0000-4000-8000-000000000001/download`);
    expect(download).toHaveAttribute("download");
    expect(expired).toHaveTextContent("User");
    expect(expired).toHaveTextContent("Expired");
    expect(within(expired!).queryByRole("link")).not.toBeInTheDocument();
    expect(failed).toHaveTextContent("Failed");
    expect(failed).toHaveTextContent("too_large");
    expect(within(failed!).queryByRole("link")).not.toBeInTheDocument();
    // As a screen-reader user meets it.
    expect(headingProblems(document.body)).toEqual([]);
    expect(unnamedControls(document.body)).toEqual([]);
    expect(ambiguousControls(document.body)).toEqual([]);
    expect(labelInNameProblems(document.body)).toEqual([]);
  });

  it("reads the list again while an export is being prepared, and stops once it's ready", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let status: DataExport["status"] = "queued";
    const api = mockApi({ [`GET ${EXPORTS}`]: () => ({ status: 200, body: { results: [makeExport({ status, size: status === "ready" ? 48_640 : null })] } }) });
    renderWithProviders(<DataRequestsPage />, { viewer: adminViewer });
    expect(await screen.findByText("Preparing")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Download/ })).not.toBeInTheDocument();
    status = "ready";
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS);
    });
    expect(await screen.findByRole("link", { name: "Download DSR-2026-014" })).toBeInTheDocument();
    const reads = api.callsTo("GET", EXPORTS).length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MS * 3);
    });
    expect(api.callsTo("GET", EXPORTS)).toHaveLength(reads);
  });

  it("says when there are none, and explains a refusal", async () => {
    mockApi({ [`GET ${EXPORTS}`]: { status: 200, body: { results: [] } } });
    const first = renderWithProviders(<DataRequestsPage />, { viewer: adminViewer });
    expect(await screen.findByText("No data requests yet")).toBeInTheDocument();
    first.unmount();
    mockApi({ [`GET ${EXPORTS}`]: apiError(403, "support_session_active", "Not during a support session.") });
    renderWithProviders(<DataRequestsPage />, { viewer: adminViewer });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("You can't manage data requests");
    expect(alert).toHaveTextContent("Not available during a support session. Exit it first.");
  });
});

describe("a user's privacy actions, from their details", () => {
  const LIST = "GET /api/v1/admin/users";
  const rahul = makeAdminUser();
  const gone = makeAdminUser({ status: "deactivated", status_label: "Deactivated", deactivated_at: "2026-10-01T04:30:00Z" });
  const PSEUDONYMISE = `/api/v1/admin/privacy/users/${rahul.id}/pseudonymise`;

  function routesFor(user: typeof rahul, extra: Parameters<typeof mockApi>[0] = {}) {
    return mockApi({
      [LIST]: { status: 200, body: { results: [user], next: null, previous: null } },
      [`GET /api/v1/admin/users/${user.id}`]: { status: 200, body: user },
      ...extra,
    });
  }

  async function openDetails() {
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Rahul Sharma" }));
    return { user, drawer: screen.getByRole("dialog", { name: "Rahul Sharma" }) };
  }

  it("only for administrators who handle privacy requests; pseudonymising only once deactivated", async () => {
    routesFor(rahul);
    const noPrivacy = makeViewer({ ...adminViewer, capabilities: adminViewer.capabilities.filter((c) => c !== "privacy.manage") });
    const first = renderWithProviders(<UsersPage />, { viewer: noPrivacy });
    let { drawer } = await openDetails();
    expect(within(drawer).queryByRole("button", { name: "Export data" })).not.toBeInTheDocument();
    first.unmount();

    renderWithProviders(<UsersPage />, { viewer: adminViewer });
    ({ drawer } = await openDetails());
    expect(within(drawer).getByRole("button", { name: "Export data" })).toBeInTheDocument();
    expect(within(drawer).queryByRole("button", { name: "Pseudonymise…" })).not.toBeInTheDocument();
    expect(drawer).toHaveTextContent("Only a deactivated user can be pseudonymised.");
  });

  it("exports a user's data with a reference and a confirmed identity check", async () => {
    const api = routesFor(rahul, {
      [`POST ${EXPORTS}`]: (call: RecordedCall) => ({ status: 202, body: makeExport({ status: "queued", ...(call.body as object) }) }),
    });
    renderWithProviders(<UsersPage />, { viewer: adminViewer });
    const { user, drawer } = await openDetails();
    await user.click(within(drawer).getByRole("button", { name: "Export data" }));
    const dialog = screen.getByRole("dialog", { name: "Export Rahul Sharma's data" });
    await user.type(within(dialog).getByRole("textbox", { name: "Request reference" }), "HR-2026-3");
    await user.click(within(dialog).getByRole("button", { name: "Request export" }));
    expect(api.callsTo("POST", EXPORTS)).toHaveLength(0); // the identity check isn't confirmed yet
    await user.click(within(dialog).getByRole("checkbox", { name: "I have verified the requester's identity" }));
    await user.click(within(dialog).getByRole("button", { name: "Request export" }));
    expect(await within(dialog).findByText("Export requested")).toBeInTheDocument();
    expect(api.callsTo("POST", EXPORTS)[0]!.body).toEqual({
      subject_type: "user",
      subject_id: rahul.id,
      reference: "HR-2026-3",
      identity_verified: true,
    });
  });

  it("pseudonymises a deactivated user only once their exact email is typed, then shows what was removed", async () => {
    const api = routesFor(gone, {
      [`POST ${PSEUDONYMISE}`]: {
        status: 200,
        body: {
          conversations: 3,
          audit_details: 12,
          support_reasons: 0,
          tokens_revoked: 1,
          support_sessions_ended: 0,
          throttle_events: 0,
          queued_payloads: 0,
          access_windows: 0,
        },
      },
    });
    renderWithProviders(<UsersPage />, { viewer: adminViewer });
    const { user, drawer } = await openDetails();
    await user.click(within(drawer).getByRole("button", { name: "Pseudonymise…" }));
    const dialog = screen.getByRole("alertdialog", { name: "Pseudonymise Rahul Sharma?" });
    expect(dialog).toHaveAccessibleDescription(expect.stringContaining("This can't be undone."));
    expect(dialog).toHaveAccessibleDescription(expect.stringContaining("Their records stay attributed"));
    const email = within(dialog).getByRole("textbox", { name: "Type their email to confirm" });

    await user.click(within(dialog).getByRole("button", { name: "Pseudonymise" }));
    expect(email).toHaveAccessibleDescription(expect.stringContaining("Type the account's email address to confirm."));
    await user.type(email, "rahul@example.tes");
    await user.click(within(dialog).getByRole("button", { name: "Pseudonymise" }));
    expect(email).toHaveAccessibleDescription(expect.stringContaining("That isn't this account's email address."));
    expect(api.callsTo("POST", PSEUDONYMISE)).toHaveLength(0);

    await user.type(email, "t");
    await user.click(within(dialog).getByRole("button", { name: "Pseudonymise" }));
    expect(await within(dialog).findByText("Account pseudonymised")).toBeInTheDocument();
    expect(api.callsTo("POST", PSEUDONYMISE)[0]!.body).toEqual({ confirm_email: "rahul@example.test" });
    expect(dialog).toHaveTextContent("Ask Arkray conversations deleted: 3");
    expect(dialog).toHaveTextContent("Expiring audit details deleted: 12");
    expect(dialog).not.toHaveTextContent("Support sessions ended");
    // The list (and their details) are read again: they now show the pseudonym.
    await waitFor(() => expect(api.callsTo("GET", "/api/v1/admin/users").length).toBeGreaterThan(1));
  });

  it("shows the server's refusal (a user who still owns current work)", async () => {
    routesFor(gone, {
      [`POST ${PSEUDONYMISE}`]: apiError(422, "business_rule_violation", "Rahul Sharma still owns 2 open deals. Reassign them first."),
    });
    renderWithProviders(<UsersPage />, { viewer: adminViewer });
    const { user, drawer } = await openDetails();
    await user.click(within(drawer).getByRole("button", { name: "Pseudonymise…" }));
    const dialog = screen.getByRole("alertdialog", { name: "Pseudonymise Rahul Sharma?" });
    await user.type(within(dialog).getByRole("textbox", { name: "Type their email to confirm" }), "Rahul@Example.test");
    await user.click(within(dialog).getByRole("button", { name: "Pseudonymise" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("still owns 2 open deals");
  });

  it("the email confirmation ignores case and surrounding spaces, nothing else", () => {
    expect(confirmsEmail(" Rahul@Example.TEST ", "rahul@example.test")).toBe(true);
    expect(confirmsEmail("rahul@example.tes", "rahul@example.test")).toBe(false);
    expect(confirmsEmail("", "rahul@example.test")).toBe(false);
  });
});
