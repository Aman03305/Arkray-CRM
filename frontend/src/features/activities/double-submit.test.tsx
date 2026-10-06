/**
 * Two activations in the same moment (a machine-speed double click, Enter then a click) send
 * one request (final audit UI-5). The mutation's pending state reaches the screen a tick
 * late, so each click below is fired before the first one's request could have changed
 * anything: what guards the second is the synchronous single-flight guard.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivitiesView, ActivityView, OpportunityView } from "@/features/workspace/views";
import type { Activity, Note } from "@/lib/api/types";
import { asListItem, makeActivity, makeAttachment, makeDealNote, makeNote, NOTE_ID, page, SUMMARY, TASK_ID } from "@/test/activity-fixtures";
import { RAHUL_ID, salesViewer } from "@/test/fixtures";
import { makeOpportunity, OPPORTUNITY_ID, PIPELINE_ROUTES } from "@/test/pipeline-fixtures";
import { apiError, mockApi, renderWithProviders, type RecordedCall } from "@/test/render";

import { DealNotes } from "./DealNotes";
import { forgetActivityListState } from "./list-state";

const nav = vi.hoisted(() => ({ pathname: "/activities/x" }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
}));

const ME = "/api/v1/workspaces/me";
const LIST = `${ME}/activities`;
const DEAL = `${ME}/opportunities/${OPPORTUNITY_ID}`;
const activityUrl = (id: string, action = "") => `${ME}/activities/${id}${action ? `/${action}` : ""}`;
const SALES = { ...salesViewer, id: RAHUL_ID };
const SECOND_TASK_ID = "a1c1e000-0000-4000-8000-0000000000b1";

/** Two clicks with nothing in between: no re-render, no tick for the first to show. */
function doubleClick(element: HTMLElement) {
  fireEvent.click(element);
  fireEvent.click(element);
}

/** A versioned write that succeeds once per version, like the API: a replay of the same
 * version is a conflict. */
function versioned(start: number, reply: (version: number) => Activity) {
  let version = start;
  return (call: RecordedCall) => {
    if ((call.body as { version: number }).version !== version) return apiError(409, "conflict", "Changed.");
    version += 1;
    return { status: 200, body: reply(version) };
  };
}

beforeEach(() => {
  nav.pathname = `/activities/${TASK_ID}`;
  forgetActivityListState();
});

describe("an activity's page", () => {
  it("Complete clicked twice completes once and says so (no conflict with itself)", async () => {
    let current = makeActivity({ version: 3 });
    const api = mockApi({
      [`GET ${activityUrl(TASK_ID)}`]: () => ({ status: 200, body: current }),
      [`POST ${activityUrl(TASK_ID, "complete")}`]: versioned(3, (version) => (current = makeActivity({ status: "completed", completable: false, version }))),
    });
    renderWithProviders(<ActivityView activityId={TASK_ID} />, { viewer: SALES });
    doubleClick(await screen.findByRole("button", { name: "Complete" }));
    expect(await screen.findByText("Marked as completed.")).toBeInTheDocument();
    expect(api.callsTo("POST", activityUrl(TASK_ID, "complete"))).toHaveLength(1);
    expect(screen.queryByText(/Someone else changed this/)).not.toBeInTheDocument();
  });

  it("Save changes twice in the edit dialog saves once and closes (no conflict with itself)", async () => {
    let current = makeActivity({ version: 1 });
    const api = mockApi({
      [`GET ${activityUrl(TASK_ID)}`]: () => ({ status: 200, body: current }),
      [`PATCH ${activityUrl(TASK_ID)}`]: versioned(1, (version) => (current = makeActivity({ title: "Call back", version }))),
    });
    renderWithProviders(<ActivityView activityId={TASK_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const dialog = screen.getByRole("dialog", { name: "Edit task" });
    const subject = within(dialog).getByRole("textbox", { name: "Subject" });
    await user.clear(subject);
    await user.type(subject, "Call back");
    doubleClick(within(dialog).getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(api.callsTo("PATCH", activityUrl(TASK_ID))).toHaveLength(1);
  });

  it("a note saved twice on its page is saved once, with no conflict banner", async () => {
    nav.pathname = `/activities/${NOTE_ID}`;
    let current = makeNote({ version: 1 });
    const api = mockApi({
      [`GET ${activityUrl(NOTE_ID)}`]: () => ({ status: 200, body: current }),
      [`PATCH ${activityUrl(NOTE_ID)}`]: versioned(1, (version) => (current = makeNote({ description: "Prefers evening calls.", version }))),
    });
    renderWithProviders(<ActivityView activityId={NOTE_ID} />, { viewer: SALES });
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit note" }));
    const box = screen.getByRole("textbox", { name: "Note text" });
    await user.clear(box);
    await user.type(box, "Prefers evening calls.");
    doubleClick(screen.getByRole("button", { name: "Save note" }));
    await waitFor(() => expect(screen.queryByRole("textbox")).not.toBeInTheDocument(), { timeout: 5000 });
    expect(screen.getByText("Prefers evening calls.")).toBeInTheDocument();
    expect(api.callsTo("PATCH", activityUrl(NOTE_ID))).toHaveLength(1);
    expect(screen.queryByText(/changed meanwhile|changed it meanwhile/i)).not.toBeInTheDocument();
  });
});

describe("the Activities list", () => {
  it("Complete twice on one row sends once; Complete on another row isn't held up", async () => {
    nav.pathname = "/activities";
    const first = makeActivity({ version: 3 });
    const second = makeActivity({ id: SECOND_TASK_ID, title: "Book the demo", version: 7 });
    const api = mockApi({
      [`GET ${LIST}`]: { status: 200, body: page([asListItem(first), asListItem(second)]) },
      [`GET ${ME}/activity-summary`]: { status: 200, body: SUMMARY },
      [`POST ${activityUrl(TASK_ID, "complete")}`]: versioned(3, (version) => makeActivity({ status: "completed", version })),
      [`POST ${activityUrl(SECOND_TASK_ID, "complete")}`]: versioned(7, (version) => ({ ...second, status: "completed", version })),
    });
    renderWithProviders(<ActivitiesView />, { viewer: SALES });
    const [complete] = await screen.findAllByRole("button", { name: "Complete Send the revised quotation" });
    doubleClick(complete!);
    fireEvent.click(screen.getAllByRole("button", { name: "Complete Book the demo" })[0]!);
    await waitFor(() => expect(api.callsTo("POST", activityUrl(SECOND_TASK_ID, "complete"))).toHaveLength(1));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/completed/));
    expect(api.callsTo("POST", activityUrl(TASK_ID, "complete"))).toHaveLength(1);
    expect(screen.queryByText(/changed by someone else/)).not.toBeInTheDocument();
  });
});

describe("a deal's Open work and Notes", () => {
  it("Complete twice in Open work completes once", async () => {
    nav.pathname = `/pipeline/${OPPORTUNITY_ID}`;
    const task = makeActivity({ version: 3 });
    const api = mockApi({
      ...PIPELINE_ROUTES,
      [`GET ${DEAL}`]: { status: 200, body: makeOpportunity() },
      [`GET ${DEAL}/history`]: { status: 200, body: page([]) },
      [`GET ${DEAL}/negotiated-prices`]: { status: 200, body: page([]) },
      [`GET ${DEAL}/timeline`]: { status: 200, body: page([]) },
      [`GET ${DEAL}/notes`]: { status: 200, body: page([]) },
      [`GET ${LIST}`]: { status: 200, body: page([asListItem(task)]) },
      [`POST ${activityUrl(TASK_ID, "complete")}`]: versioned(3, (version) => makeActivity({ status: "completed", version })),
    } as Parameters<typeof mockApi>[0]);
    renderWithProviders(<OpportunityView opportunityId={OPPORTUNITY_ID} />, { viewer: SALES });
    doubleClick(await screen.findByRole("button", { name: "Complete Send the revised quotation" }));
    expect(await screen.findByText("Send the revised quotation completed.")).toBeInTheDocument();
    expect(api.callsTo("POST", activityUrl(TASK_ID, "complete"))).toHaveLength(1);
  });

  it("a new note with a file saved twice is created once and its file uploaded once", async () => {
    let notes: Note[] = [];
    const api = mockApi({
      [`GET ${DEAL}/notes`]: () => ({ status: 200, body: page(notes) }),
      [`POST ${LIST}`]: () => {
        notes = [makeDealNote({ description: "Quotation sent." })];
        return { status: 201, body: makeNote({ description: "Quotation sent." }) };
      },
      [`POST ${activityUrl(NOTE_ID, "attachments")}`]: () => ({ status: 201, body: makeAttachment() }),
    });
    renderWithProviders(<DealNotes workspace={{ kind: "self" }} opportunityId={OPPORTUNITY_ID} canAdd />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add note" }));
    await user.type(screen.getByLabelText("Note"), "Quotation sent.");
    await user.upload(screen.getByLabelText("Attach files"), [new File(["content"], "Quote.pdf")]);
    doubleClick(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Note saved with 1 file.")).toBeInTheDocument();
    expect(api.callsTo("POST", LIST)).toHaveLength(1);
    expect(api.callsTo("POST", activityUrl(NOTE_ID, "attachments"))).toHaveLength(1);
  });

  it("a note edited in the list and saved twice is saved once", async () => {
    let current = makeDealNote({ version: 1 });
    const api = mockApi({
      [`GET ${DEAL}/notes`]: () => ({ status: 200, body: page([current]) }),
      [`PATCH ${activityUrl(NOTE_ID)}`]: (call: RecordedCall) => {
        const body = call.body as { version: number; description: string };
        if (body.version !== current.version) return apiError(409, "conflict", "Changed.");
        current = makeDealNote({ description: body.description, version: current.version + 1 });
        return { status: 200, body: makeNote({ description: body.description, version: current.version }) };
      },
    });
    renderWithProviders(<DealNotes workspace={{ kind: "self" }} opportunityId={OPPORTUNITY_ID} canAdd />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /^Edit note/ }));
    const box = screen.getByLabelText("Edit note");
    await user.clear(box);
    await user.type(box, "Prefers evening calls.");
    doubleClick(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByLabelText("Edit note")).not.toBeInTheDocument(), { timeout: 5000 });
    expect(screen.getByText("Prefers evening calls.")).toBeInTheDocument();
    expect(api.callsTo("PATCH", activityUrl(NOTE_ID))).toHaveLength(1);
  });
});
