import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Attachment, Note } from "@/lib/api/types";
import { ATTACHMENT_ID, makeAttachment, makeDealNote, makeNote, NOTE_ID, page } from "@/test/activity-fixtures";
import { PRIYA_ID } from "@/test/fixtures";
import { OPPORTUNITY_ID } from "@/test/pipeline-fixtures";
import { apiError, mockApi, type RecordedCall, renderWithProviders } from "@/test/render";

import { DealNotes } from "./DealNotes";

const ME = "/api/v1/workspaces/me";
const NOTES = `${ME}/opportunities/${OPPORTUNITY_ID}/notes`;
const CREATE = `${ME}/activities`;
const NOTE = `${ME}/activities/${NOTE_ID}`;
const UPLOAD = `${NOTE}/attachments`;
const PRIYA = { id: PRIYA_ID, full_name: "Priya Nair", is_active: true };
const SECOND_FILE_ID = "a77ac000-0000-4000-8000-0000000000f2";

function renderNotes({ canAdd = true } = {}) {
  return renderWithProviders(<DealNotes workspace={{ kind: "self" }} opportunityId={OPPORTUNITY_ID} canAdd={canAdd} />);
}

function file(name: string, content = "content", type = "application/octet-stream"): File {
  return new File([content], name, { type });
}

/** The raw bodies the upload route received (mockApi records JSON bodies only). */
function uploadedBodies(api: ReturnType<typeof mockApi>): unknown[] {
  return api.fetchMock.mock.calls
    .filter(([input, init]) => init?.method === "POST" && String(input) === UPLOAD)
    .map(([, init]) => init?.body);
}

function noteItem(text: string): HTMLElement {
  return screen.getByText(text).closest("li")!;
}

describe("a deal's notes", () => {
  it("adds a note with text, with an idempotency key, and returns focus to Add note", async () => {
    let notes: Note[] = [];
    const api = mockApi({
      [`GET ${NOTES}`]: () => ({ status: 200, body: page(notes) }),
      [`POST ${CREATE}`]: (call: RecordedCall) => {
        const body = call.body as { description: string };
        notes = [makeDealNote({ description: body.description })];
        return { status: 201, body: makeNote({ description: body.description, opportunity: null }) };
      },
    });
    renderNotes();
    const user = userEvent.setup();
    expect(await screen.findByText("No notes yet.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add note" }));
    const box = screen.getByLabelText("Note");
    expect(box).toHaveFocus();
    expect(box).toHaveAttribute("maxLength", "10000");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Write the note first.");
    expect(api.callsTo("POST", CREATE)).toHaveLength(0);
    await user.type(box, "Budget approved.{Enter}Order next week.");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Note saved.")).toBeInTheDocument();
    const [create] = api.callsTo("POST", CREATE);
    expect(create!.body).toEqual({ type: "note", opportunity: OPPORTUNITY_ID, description: "Budget approved.\nOrder next week." });
    expect(create!.headers["Idempotency-Key"]).toMatch(/^[0-9a-f-]{36}$/);
    const text = await screen.findByText(/Budget approved\./);
    expect(text.textContent).toBe("Budget approved.\nOrder next week.");
    expect(text).toHaveClass("whitespace-pre-wrap");
    expect(screen.getByRole("button", { name: "Add note" })).toHaveFocus();
  });

  it("adds a note with two files: saved first, then each uploaded as the raw body", async () => {
    let notes: Note[] = [];
    const stored: Attachment[] = [];
    const api = mockApi({
      [`GET ${NOTES}`]: () => ({ status: 200, body: page(notes) }),
      [`POST ${CREATE}`]: () => {
        notes = [makeDealNote({ description: "Quotation sent." })];
        return { status: 201, body: makeNote({ description: "Quotation sent." }) };
      },
      [`POST ${UPLOAD}`]: (call: RecordedCall) => {
        const name = decodeURIComponent(call.headers["X-Filename"]!);
        const attachment = makeAttachment({
          id: stored.length ? SECOND_FILE_ID : ATTACHMENT_ID,
          name,
          extension: name.split(".").pop()!,
          size: 7,
        });
        stored.push(attachment);
        notes = [makeDealNote({ description: "Quotation sent.", attachments: [...stored] })];
        return { status: 201, body: attachment };
      },
    });
    renderNotes();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add note" }));
    await user.type(screen.getByLabelText("Note"), "Quotation sent.");
    const quote = file("Quote.pdf");
    const prices = file("Price list (Q3) ₹.csv");
    const picker = screen.getByLabelText("Attach files");
    expect(picker).toHaveAttribute("type", "file");
    expect(picker).toHaveAttribute("multiple");
    expect(picker).toHaveAttribute("accept", ".pdf,.png,.jpg,.jpeg,.webp,.docx,.xlsx,.csv,.txt");
    await user.upload(picker, [quote, prices]);
    const chosen = screen.getByRole("list", { name: "Files to attach" });
    expect(within(chosen).getByText("Quote.pdf")).toBeInTheDocument();
    expect(within(chosen).getAllByText("7 B")).toHaveLength(2);
    expect(within(chosen).getByRole("button", { name: "Remove Price list (Q3) ₹.csv" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Note saved with 2 files.")).toBeInTheDocument();
    const uploads = api.callsTo("POST", UPLOAD);
    expect(uploads).toHaveLength(2);
    expect(uploads.map((call) => call.headers["X-Filename"])).toEqual([
      encodeURIComponent("Quote.pdf"),
      encodeURIComponent("Price list (Q3) ₹.csv"),
    ]);
    for (const call of uploads) {
      expect(call.headers["Content-Type"]).toBe("application/octet-stream");
      expect(call.headers["X-CSRFToken"]).toBe("test-csrf-token");
    }
    const bodies = uploadedBodies(api);
    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toBe(quote);
    expect(bodies[1]).toBe(prices);
    // The note was created before the first upload.
    const order = api.calls.filter((call) => call.method === "POST").map((call) => call.path);
    expect(order).toEqual([CREATE, UPLOAD, UPLOAD]);
    const files = await screen.findByRole("list", { name: "Files" });
    expect(within(files).getByTitle("Quote.pdf")).toBeInTheDocument();
    expect(within(files).getByTitle("Price list (Q3) ₹.csv")).toBeInTheDocument();
  });

  it("catches a file of a type that isn't allowed, an empty one and an oversized one before uploading", async () => {
    mockApi({ [`GET ${NOTES}`]: { status: 200, body: page([]) } });
    renderNotes();
    const user = userEvent.setup({ applyAccept: false });
    await user.click(await screen.findByRole("button", { name: "Add note" }));
    const big = file("Scan.png");
    Object.defineProperty(big, "size", { value: 11 * 1024 * 1024 });
    await user.upload(screen.getByLabelText("Attach files"), [file("setup.exe"), file("Empty.txt", ""), big, file("Ok.txt")]);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("setup.exe: This file type isn't allowed.");
    expect(alert).toHaveTextContent("Empty.txt: This file is empty.");
    expect(alert).toHaveTextContent("Scan.png: Files can be at most 10 MB.");
    const chosen = screen.getByRole("list", { name: "Files to attach" });
    expect(within(chosen).getAllByRole("listitem")).toHaveLength(1);
    expect(within(chosen).getByText("Ok.txt")).toBeInTheDocument();
  });

  it("refuses more than 10 files on a note", async () => {
    mockApi({ [`GET ${NOTES}`]: { status: 200, body: page([]) } });
    renderNotes();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add note" }));
    const many = Array.from({ length: 11 }, (_, i) => file(`File ${i + 1}.txt`));
    await user.upload(screen.getByLabelText("Attach files"), many);
    expect(screen.getByRole("alert")).toHaveTextContent("File 11.txt: A note can have at most 10 files.");
    expect(within(screen.getByRole("list", { name: "Files to attach" })).getAllByRole("listitem")).toHaveLength(10);
    // Full: no more picking.
    expect(screen.queryByLabelText("Attach files")).not.toBeInTheDocument();
  });

  it("shows the server's file errors (400, 503) with Retry; the note stays saved", async () => {
    let storageDown = true;
    const api = mockApi({
      [`GET ${NOTES}`]: { status: 200, body: page([]) },
      [`POST ${CREATE}`]: { status: 201, body: makeNote() },
      [`POST ${UPLOAD}`]: (call: RecordedCall) => {
        const name = decodeURIComponent(call.headers["X-Filename"]!);
        if (name === "Fake.pdf") {
          return apiError(400, "invalid_input", "Check the highlighted fields.", { file: ["The file's content doesn't match its type."] });
        }
        if (storageDown) return apiError(503, "storage_unavailable", "File storage is unavailable right now. Try again in a few minutes.");
        return { status: 201, body: makeAttachment({ name }) };
      },
    });
    renderNotes();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Add note" }));
    await user.type(screen.getByLabelText("Note"), "Brochure attached.");
    await user.upload(screen.getByLabelText("Attach files"), [file("Fake.pdf"), file("Brochure.pdf")]);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Some files weren't uploaded/)).toBeInTheDocument();
    expect(screen.getByText("Note saved.", { exact: false })).toHaveFocus();
    const alerts = screen.getAllByRole("alert").map((el) => el.textContent);
    expect(alerts).toContain("The file's content doesn't match its type.");
    expect(alerts).toContain("File storage is unavailable right now. Try again in a few minutes.");
    expect(api.callsTo("POST", CREATE)).toHaveLength(1);

    storageDown = false;
    await user.click(screen.getByRole("button", { name: "Retry Brochure.pdf" }));
    await waitFor(() => expect(screen.queryByText("File storage is unavailable right now. Try again in a few minutes.")).not.toBeInTheDocument());
    expect(api.callsTo("POST", UPLOAD)).toHaveLength(3);
    expect(api.callsTo("POST", CREATE)).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Retry Fake.pdf" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.getByRole("button", { name: "Add note" })).toHaveFocus();
  });

  it("shows a 413 from the server on a note's added file", async () => {
    mockApi({
      [`GET ${NOTES}`]: { status: 200, body: page([makeDealNote()]) },
      [`POST ${UPLOAD}`]: apiError(413, "file_too_large", "Files can be at most 10 MB."),
    });
    renderNotes();
    const user = userEvent.setup();
    await screen.findByText("Prefers morning calls.");
    const item = noteItem("Prefers morning calls.");
    await user.upload(within(item).getByLabelText("Add files"), [file("Big.pdf")]);
    expect(await within(item).findByRole("alert")).toHaveTextContent("Files can be at most 10 MB.");
    expect(within(item).getByRole("button", { name: "Retry Big.pdf" })).toBeInTheDocument();
  });

  it("edits a note with its version; a conflict keeps the text and shows the latest", async () => {
    let current = makeDealNote({ version: 1 });
    const api = mockApi({
      [`GET ${NOTES}`]: () => ({ status: 200, body: page([current]) }),
      [`GET ${NOTE}`]: () => ({ status: 200, body: makeNote({ description: current.description, version: current.version }) }),
      [`PATCH ${NOTE}`]: (call: RecordedCall) => {
        const body = call.body as { version: number; description: string };
        if (body.version !== current.version) return apiError(409, "conflict", "Changed.");
        current = makeDealNote({
          description: body.description,
          version: current.version + 1,
          edited_at: "2026-10-04T05:00:00Z",
          edited_by: PRIYA,
        });
        return {
          status: 200,
          body: makeNote({ description: current.description, version: current.version, edited_at: current.edited_at, edited_by: PRIYA }),
        };
      },
    });
    renderNotes();
    const user = userEvent.setup();
    await screen.findByText("Prefers morning calls.");
    // Someone else edits it meanwhile.
    current = makeDealNote({ description: "Calls after 4 pm.", version: 2 });
    await user.click(screen.getByRole("button", { name: "Edit note" }));
    const box = screen.getByLabelText("Edit note");
    expect(box).toHaveFocus();
    expect(box).toHaveValue("Prefers morning calls.");
    await user.clear(box);
    await user.type(box, "Prefers evening calls.");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("This note changed meanwhile")).toBeInTheDocument();
    expect(screen.getByLabelText("Edit note")).toHaveValue("Prefers evening calls.");
    expect(screen.getByText("Latest version (saved)").closest("figure")).toHaveTextContent("Calls after 4 pm.");
    expect(api.callsTo("PATCH", NOTE)[0]!.body).toEqual({ version: 1, description: "Prefers evening calls." });

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Prefers evening calls.")).toBeInTheDocument();
    expect(api.callsTo("PATCH", NOTE)[1]!.body).toEqual({ version: 2, description: "Prefers evening calls." });
    expect(screen.queryByLabelText("Edit note")).not.toBeInTheDocument();
    expect(screen.getByText(/Edited by/)).toHaveTextContent("Edited by Priya Nair");
    expect(screen.getByRole("button", { name: "Edit note" })).toHaveFocus();
  });

  it("an emptied note is not saved; cancelling keeps the note and returns focus to Edit", async () => {
    const api = mockApi({ [`GET ${NOTES}`]: { status: 200, body: page([makeDealNote()]) } });
    renderNotes();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit note" }));
    await user.clear(screen.getByLabelText("Edit note"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Write the note first.");
    expect(screen.getByLabelText("Edit note")).toHaveFocus();
    await user.type(screen.getByLabelText("Edit note"), "Changed.");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByText("Prefers morning calls.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit note" })).toHaveFocus();
    expect(api.callsTo("PATCH", NOTE)).toHaveLength(0);
  });

  it("shows who edited a note (an administrator, not the author)", async () => {
    mockApi({
      [`GET ${NOTES}`]: {
        status: 200,
        body: page([makeDealNote({ edited_at: "2026-10-02T05:00:00Z", edited_by: PRIYA, version: 2 })]),
      },
    });
    renderNotes();
    await screen.findByText("Prefers morning calls.");
    const item = noteItem("Prefers morning calls.");
    expect(within(item).getByText("Rahul Sharma")).toBeInTheDocument();
    expect(within(item).getByText(/Edited by/)).toHaveTextContent("Edited by Priya Nair");
  });

  it("shows text as text, never as HTML", async () => {
    mockApi({ [`GET ${NOTES}`]: { status: 200, body: page([makeDealNote({ description: "<b>bold</b> <img src=x onerror=alert(1)>" })]) } });
    renderNotes();
    expect(await screen.findByText("<b>bold</b> <img src=x onerror=alert(1)>")).toBeInTheDocument();
    expect(document.querySelector("b")).toBeNull();
  });

  it("offers no Edit, Add files or Remove without can_edit, and no Add note without canAdd", async () => {
    mockApi({ [`GET ${NOTES}`]: { status: 200, body: page([makeDealNote({ can_edit: false, attachments: [makeAttachment()] })]) } });
    renderNotes({ canAdd: false });
    expect(await screen.findByTitle("Quotation.pdf")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit note" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Add files")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Remove/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add note" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Download/ })).toBeInTheDocument();
  });

  it("renders files: icon or thumbnail, size, download link and scan states", async () => {
    mockApi({
      [`GET ${NOTES}`]: {
        status: 200,
        body: page([
          makeDealNote({
            attachments: [
              makeAttachment(),
              makeAttachment({ id: SECOND_FILE_ID, name: "Site photo.jpg", extension: "jpg", content_type: "image/jpeg", previewable: true, size: 2048 }),
              makeAttachment({ id: "a77ac000-0000-4000-8000-0000000000f3", name: "Pending.pdf", scan_status: "pending", downloadable: false }),
              makeAttachment({ id: "a77ac000-0000-4000-8000-0000000000f4", name: "Blocked.docx", extension: "docx", scan_status: "rejected", downloadable: false }),
            ],
          }),
        ]),
      },
    });
    renderNotes();
    const files = await screen.findByRole("list", { name: "Files" });
    const rows = within(files).getAllByRole("listitem");
    expect(rows).toHaveLength(4);

    const pdf = rows[0]!;
    expect(within(pdf).getByText("1.2 MB")).toBeInTheDocument();
    const download = within(pdf).getByRole("link", { name: "Download Quotation.pdf" });
    expect(download).toHaveAttribute("href", `${ME}/attachments/${ATTACHMENT_ID}/download`);
    expect(download).toHaveAttribute("download");

    const photo = rows[1]!;
    const thumbnail = within(photo).getByRole("img", { name: "Site photo.jpg" });
    expect(thumbnail).toHaveAttribute("src", `${ME}/attachments/${SECOND_FILE_ID}/preview`);
    expect(thumbnail).toHaveAttribute("loading", "lazy");
    expect(thumbnail).toHaveClass("object-cover");
    expect(thumbnail.closest("a")).toHaveAttribute("href", `${ME}/attachments/${SECOND_FILE_ID}/preview`);
    expect(within(photo).getByText("2 KB")).toBeInTheDocument();

    const pending = rows[2]!;
    expect(within(pending).getByText(/Checking for viruses…/)).toBeInTheDocument();
    expect(within(pending).queryByRole("link")).not.toBeInTheDocument();

    const blocked = rows[3]!;
    expect(within(blocked).getByText(/Blocked by virus scan/)).toBeInTheDocument();
    expect(within(blocked).queryByRole("link")).not.toBeInTheDocument();
    // Never an internal id or storage key on screen.
    expect(files.textContent).not.toContain(ATTACHMENT_ID);
  });

  it("removes a file after a danger-toned confirmation", async () => {
    let attachments = [makeAttachment()];
    const api = mockApi({
      [`GET ${NOTES}`]: () => ({ status: 200, body: page([makeDealNote({ attachments })]) }),
      [`DELETE ${ME}/attachments/${ATTACHMENT_ID}`]: () => {
        attachments = [];
        return { status: 204 };
      },
    });
    renderNotes();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Remove Quotation.pdf" }));
    const dialog = screen.getByRole("alertdialog", { name: "Remove this file?" });
    expect(within(dialog).getByText(/will be removed from this note/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    const confirm = within(dialog).getByRole("button", { name: "Remove file" });
    expect(confirm).toHaveClass("bg-red-600");
    await user.click(confirm);
    await waitFor(() => expect(screen.queryByTitle("Quotation.pdf")).not.toBeInTheDocument());
    expect(api.callsTo("DELETE", `${ME}/attachments/${ATTACHMENT_ID}`)).toHaveLength(1);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(noteItem("Prefers morning calls.")).toHaveFocus();
  });

  it("adds files to an existing note", async () => {
    let attachments: Attachment[] = [];
    const api = mockApi({
      [`GET ${NOTES}`]: () => ({ status: 200, body: page([makeDealNote({ attachments })]) }),
      [`POST ${UPLOAD}`]: (call: RecordedCall) => {
        const stored = makeAttachment({ name: decodeURIComponent(call.headers["X-Filename"]!), extension: "txt", size: 7 });
        attachments = [stored];
        return { status: 201, body: stored };
      },
    });
    renderNotes();
    const user = userEvent.setup();
    await screen.findByText("Prefers morning calls.");
    const item = noteItem("Prefers morning calls.");
    const minutes = file("Minutes.txt");
    await user.upload(within(item).getByLabelText("Add files"), [minutes]);
    expect(await within(item).findByRole("link", { name: "Download Minutes.txt" })).toBeInTheDocument();
    const bodies = uploadedBodies(api);
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toBe(minutes);
    expect(within(item).queryByRole("list", { name: "Uploads" })).not.toBeInTheDocument();
  });

  it("loads older notes with Show older and moves focus to the first one added", async () => {
    const older = makeDealNote({ id: "a1c1e000-0000-4000-8000-0000000000b9", description: "First contact." });
    const api = mockApi({
      [`GET ${NOTES}`]: (call: RecordedCall) =>
        call.query.get("cursor") === "older"
          ? { status: 200, body: page([older]) }
          : { status: 200, body: page([makeDealNote()], `http://testserver${NOTES}?cursor=older&page_size=20`) },
    });
    renderNotes();
    const user = userEvent.setup();
    await screen.findByText("Prefers morning calls.");
    expect(screen.queryByText("First contact.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show older" }));
    expect(await screen.findByText("First contact.")).toBeInTheDocument();
    const calls = api.callsTo("GET", NOTES);
    expect(calls[0]!.query.get("page_size")).toBe("20");
    expect(calls[1]!.query.get("cursor")).toBe("older");
    await waitFor(() => expect(noteItem("First contact.")).toHaveFocus());
    expect(screen.queryByRole("button", { name: "Show older" })).not.toBeInTheDocument();
  });

  it("cuts long file names with an ellipsis instead of overflowing (full name on hover)", async () => {
    const long = `${"Annual maintenance contract renewal ".repeat(6)}final.pdf`;
    mockApi({ [`GET ${NOTES}`]: { status: 200, body: page([makeDealNote({ attachments: [makeAttachment({ name: long })] })]) } });
    renderNotes();
    const name = await screen.findByTitle(long);
    expect(name).toHaveClass("truncate");
    expect(name.parentElement).toHaveClass("min-w-0", "flex-1");
    expect(name.closest("li")).toHaveClass("min-w-0");
  });

  it("explains when the notes can't be loaded, with Try again", async () => {
    let fail = true;
    mockApi({
      [`GET ${NOTES}`]: () => (fail ? apiError(500, "server_error", "Boom.") : { status: 200, body: page([makeDealNote()]) }),
    });
    renderNotes();
    const user = userEvent.setup();
    expect(await screen.findByRole("alert")).toHaveTextContent("Notes couldn't be loaded.");
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Prefers morning calls.")).toBeInTheDocument();
  });
});
