"use client";

import { useInfiniteQuery, useMutation } from "@tanstack/react-query";
import { Pencil, Plus } from "lucide-react";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { Skeleton } from "@/components/ui/Skeleton";
import { cursorOf } from "@/features/leads/api";
import { PersonName } from "@/features/leads/LeadBits";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { Activity, ActivityCreateRequest, Attachment, Note } from "@/lib/api/types";
import { randomUuid } from "@/lib/random";
import type { Workspace } from "@/lib/workspace";

import { activitiesApi, activityKeys } from "./api";
import { MAX_FILES_PER_NOTE, pickFiles } from "./attachments";
import { useDealNotesCache, useUploadQueue } from "./deal-notes";
import { useActivityWriteSync } from "./hooks";
import { AttachmentList, FilePicker, PickedFiles, RefusedFiles, RelativeTime, UploadList } from "./NoteFiles";

const NOTE_MAX = 10_000;
const TEXTAREA =
  "block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 read-only:bg-slate-50 focus-visible:outline-brand-600";

export interface DealNotesProps {
  workspace: Workspace;
  opportunityId: string;
  /** The viewer may add notes to this deal (each note says itself whether it may be changed). */
  canAdd: boolean;
}

const savedWith = (count: number) => (count === 1 ? "Note saved with 1 file." : `Note saved with ${count} files.`);

/**
 * A deal's Notes: whole texts newest first ("Show older" adds the next page), who wrote and
 * last edited each, and its files. Notes are added here with files; each note's author (or
 * an administrator) edits it and adds or removes its files.
 */
export function DealNotes({ workspace, opportunityId, canAdd }: DealNotesProps) {
  const headingId = useId();
  const composerId = useId();
  const [composing, setComposing] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  // Closing the composer brings focus back to "Add note".
  const addArea = useRef<HTMLDivElement>(null);
  const focusAdd = useRef(false);
  useEffect(() => {
    if (composing || !focusAdd.current) return;
    focusAdd.current = false;
    addArea.current?.querySelector("button")?.focus();
  }, [composing]);

  const notes = useInfiniteQuery({
    queryKey: activityKeys.dealNotes(workspace, opportunityId),
    queryFn: ({ pageParam }) => activitiesApi.dealNotes(workspace, opportunityId, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => cursorOf(last.next),
  });
  const items = notes.data?.pages.flatMap((page) => page.results) ?? [];
  const refused = isApiError(notes.error, 404) || isApiError(notes.error, 403);
  // "Show older": focus moves to the first note it added.
  const list = useRef<HTMLOListElement>(null);
  const focusFrom = useRef<number | null>(null);
  useEffect(() => {
    if (focusFrom.current === null || notes.isFetchingNextPage) return;
    (list.current?.children[focusFrom.current] as HTMLElement | undefined)?.focus();
    focusFrom.current = null;
  }, [notes.isFetchingNextPage, items.length]);

  return (
    <section aria-labelledby={headingId} className="min-w-0 rounded-lg border border-slate-200 bg-white p-4 sm:p-5">
      <div className="mb-3 flex min-h-8 flex-wrap items-center gap-x-3 gap-y-1">
        <h2 id={headingId} className="text-sm font-semibold text-slate-900">
          Notes
        </h2>
        <span aria-live="polite" className="text-xs text-emerald-700">
          {announcement}
        </span>
        {canAdd && !composing ? (
          <div ref={addArea} className="ml-auto">
            <Button
              size="sm"
              icon={<Plus aria-hidden="true" className="size-4" />}
              onClick={() => {
                setAnnouncement("");
                setComposing(true);
              }}
            >
              Add note
            </Button>
          </div>
        ) : null}
      </div>
      {composing ? (
        <NoteComposer
          id={composerId}
          workspace={workspace}
          opportunityId={opportunityId}
          onClose={(message) => {
            focusAdd.current = true;
            setComposing(false);
            setAnnouncement(message ?? "");
          }}
        />
      ) : null}
      {notes.isError && (refused || items.length === 0) ? (
        <Alert
          tone="error"
          requestId={describeError(notes.error).requestId}
          action={
            refused ? null : (
              <Button variant="secondary" size="sm" onClick={() => void notes.refetch()} loading={notes.isFetching}>
                Try again
              </Button>
            )
          }
        >
          Notes couldn&apos;t be loaded. {describeError(notes.error).message}
        </Alert>
      ) : notes.isPending ? (
        <div aria-busy="true" className="space-y-2">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
          <span className="sr-only">Loading notes</span>
        </div>
      ) : items.length === 0 ? (
        composing ? null : <p className="text-sm text-slate-500">No notes yet.</p>
      ) : (
        <>
          {notes.isError ? (
            <p role="status" className="mb-2 text-xs text-red-700">
              The latest notes couldn&apos;t be loaded; showing what was loaded before.
            </p>
          ) : null}
          <ol ref={list} aria-labelledby={headingId} className="min-w-0 divide-y divide-slate-100">
            {items.map((note) => (
              <DealNoteItem key={note.id} workspace={workspace} opportunityId={opportunityId} note={note} />
            ))}
          </ol>
          {notes.hasNextPage ? (
            <div className="mt-4 flex justify-center">
              <Button
                variant="secondary"
                size="sm"
                loading={notes.isFetchingNextPage}
                onClick={() => {
                  focusFrom.current = items.length;
                  void notes.fetchNextPage();
                }}
              >
                Show older
              </Button>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

/**
 * A new note with files: the note is saved first (retried with the same idempotency key),
 * then each file is uploaded to it in turn. A file that fails shows why, with Retry; the
 * note stays saved either way.
 */
function NoteComposer({
  id,
  workspace,
  opportunityId,
  onClose,
}: {
  id: string;
  workspace: Workspace;
  opportunityId: string;
  /** With what to announce ("Note saved."), or null when cancelled. */
  onClose: (message: string | null) => void;
}) {
  const sync = useActivityWriteSync(workspace);
  const fieldId = useId();
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [refusedFiles, setRefusedFiles] = useState<string[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [noteId, setNoteId] = useState<string | null>(null);
  const queue = useUploadQueue(workspace, opportunityId);
  const box = useRef<HTMLTextAreaElement>(null);
  const savedLine = useRef<HTMLParagraphElement>(null);
  const idempotency = useRef<{ body: string; key: string } | null>(null);
  useEffect(() => {
    if (noteId) savedLine.current?.focus();
  }, [noteId]);

  const save = useMutation({
    mutationFn: (body: ActivityCreateRequest) => {
      const serialised = JSON.stringify(body);
      if (idempotency.current?.body !== serialised) idempotency.current = { body: serialised, key: randomUuid() };
      return activitiesApi.create(workspace, body, idempotency.current.key);
    },
    onSuccess: (note) => {
      sync(note);
      idempotency.current = null;
    },
  });

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    if (!text.trim()) {
      setProblem("Write the note first.");
      box.current?.focus();
      return;
    }
    setProblem(null);
    const toUpload = files;
    let note: Activity;
    try {
      note = await save.mutateAsync({ type: "note", opportunity: opportunityId, description: text });
    } catch {
      box.current?.focus();
      return;
    }
    if (!toUpload.length) {
      onClose("Note saved.");
      return;
    }
    setNoteId(note.id);
    if (await queue.start(note.id, toUpload)) onClose(savedWith(toUpload.length));
  };

  const serverProblem = save.isError ? (fieldErrors(save.error).description?.join(" ") ?? describeError(save.error).message) : null;
  const message = problem ?? serverProblem;

  if (noteId) {
    const failed = queue.items.filter((item) => item.status === "failed").length;
    return (
      <div id={id} className="mb-5 space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3">
        <p ref={savedLine} tabIndex={-1} className="text-sm font-medium text-slate-900 focus:outline-none">
          Note saved.{queue.busy ? " Uploading files…" : failed ? " Some files weren't uploaded." : ""}
        </p>
        <UploadList
          items={queue.items}
          busy={queue.busy}
          onRetry={(item) => {
            void queue.retry(noteId, item).then((stored) => {
              const others = queue.items.filter((entry) => entry.key !== item.key && entry.status === "failed");
              if (stored && others.length === 0) onClose(savedWith(queue.items.length));
            });
          }}
        />
        <div className="flex justify-end">
          <Button variant="secondary" size="sm" loading={queue.busy} onClick={() => onClose("Note saved.")}>
            Done
          </Button>
        </div>
      </div>
    );
  }

  return (
    <form id={id} onSubmit={(event) => void submit(event)} noValidate className="mb-5 space-y-3 rounded-md border border-slate-200 bg-slate-50 p-3">
      <div>
        <label htmlFor={fieldId} className="mb-1.5 block text-sm font-medium text-slate-700">
          Note
        </label>
        <textarea
          ref={box}
          id={fieldId}
          rows={4}
          maxLength={NOTE_MAX}
          value={text}
          autoFocus
          // Read-only while saving: what is typed then would not be in the saved note.
          readOnly={save.isPending}
          aria-busy={save.isPending || undefined}
          onChange={(event) => setText(event.target.value)}
          aria-invalid={message ? true : undefined}
          aria-describedby={message ? `${fieldId}-error` : undefined}
          className={TEXTAREA}
        />
        {message ? (
          <p id={`${fieldId}-error`} role="alert" className="mt-1.5 text-xs text-red-600">
            {message}
          </p>
        ) : null}
      </div>
      <PickedFiles files={files} disabled={save.isPending} onRemove={(index) => setFiles((list) => list.filter((_, i) => i !== index))} />
      <RefusedFiles messages={refusedFiles} />
      <div className="flex flex-wrap items-center gap-2">
        {files.length < MAX_FILES_PER_NOTE ? (
          <FilePicker
            label="Attach files"
            disabled={save.isPending}
            onPick={(picked) => {
              const { accepted, refused } = pickFiles(picked, MAX_FILES_PER_NOTE - files.length);
              setRefusedFiles(refused);
              setFiles((list) => [...list, ...accepted]);
            }}
          />
        ) : null}
        <div className="ml-auto flex gap-2">
          <Button variant="secondary" size="sm" disabled={save.isPending} onClick={() => onClose(null)}>
            Cancel
          </Button>
          <Button type="submit" size="sm" loading={save.isPending}>
            Save
          </Button>
        </div>
      </div>
    </form>
  );
}

/** One note: its text (or the editor), its files, and what its author may do with them. */
function DealNoteItem({ workspace, opportunityId, note }: { workspace: Workspace; opportunityId: string; note: Note }) {
  const cache = useDealNotesCache(workspace, opportunityId);
  const queue = useUploadQueue(workspace, opportunityId, { dropDone: true });
  const [editing, setEditing] = useState(false);
  const [refusedFiles, setRefusedFiles] = useState<string[]>([]);
  const [removing, setRemoving] = useState<Attachment | null>(null);
  const [status, setStatus] = useState("");
  const item = useRef<HTMLLIElement>(null);
  const actions = useRef<HTMLDivElement>(null);
  // Where focus goes once the editor or the confirmation closes.
  const focusAfter = useRef<"edit" | "note" | null>(null);
  useEffect(() => {
    if (editing || removing || !focusAfter.current) return;
    const target = focusAfter.current;
    focusAfter.current = null;
    if (target === "edit") actions.current?.querySelector("button")?.focus();
    else item.current?.focus();
  }, [editing, removing]);

  const remove = useMutation({
    mutationFn: (file: Attachment) => activitiesApi.removeFile(workspace, file.id),
    onSuccess: (_, file) => removed(file),
    onError: (error, file) => {
      if (isApiError(error, 404)) removed(file); // already gone
    },
  });
  function removed(file: Attachment) {
    cache.patch(note.id, (current) => ({ ...current, attachments: current.attachments.filter((a) => a.id !== file.id) }));
    cache.refresh(note.id);
    focusAfter.current = "note";
    setRemoving(null);
    setStatus("File removed.");
  }

  const pending = queue.items.filter((entry) => entry.status !== "failed").length;
  const room = MAX_FILES_PER_NOTE - note.attachments.length - pending;
  const editor = note.edited_by;

  return (
    <li ref={item} tabIndex={-1} className="min-w-0 py-4 first:pt-0 last:pb-0 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-600">
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-slate-900 [overflow-wrap:anywhere]">
            <PersonName person={note.created_by} />
            <span className="font-normal text-slate-500">
              {" "}
              · <RelativeTime iso={note.created_at} />
            </span>
          </p>
          {note.edited_at ? (
            <p className="text-xs text-slate-500 [overflow-wrap:anywhere]">
              Edited{editor ? (
                <>
                  {" "}
                  by <PersonName person={editor} />
                </>
              ) : null}{" "}
              · <RelativeTime iso={note.edited_at} />
            </p>
          ) : null}
        </div>
        {note.can_edit && !editing ? (
          <div ref={actions} className="flex shrink-0 items-center gap-1.5">
            <Button
              variant="ghost"
              size="sm"
              icon={<Pencil aria-hidden="true" className="size-3.5" />}
              onClick={() => {
                setStatus("");
                setEditing(true);
              }}
            >
              Edit <span className="sr-only">note</span>
            </Button>
            {room > 0 ? (
              <FilePicker
                label="Add files"
                disabled={queue.busy}
                onPick={(picked) => {
                  const { accepted, refused } = pickFiles(picked, room);
                  setRefusedFiles(refused);
                  setStatus("");
                  if (accepted.length) void queue.start(note.id, accepted);
                }}
              />
            ) : null}
          </div>
        ) : null}
      </div>
      {editing ? (
        <NoteEditor
          workspace={workspace}
          opportunityId={opportunityId}
          note={note}
          onDone={(saved) => {
            focusAfter.current = "edit";
            setEditing(false);
            setStatus(saved ? "Note updated." : "");
          }}
        />
      ) : (
        <p className="mt-1.5 whitespace-pre-wrap break-words text-sm text-slate-900 [overflow-wrap:anywhere]">{note.description}</p>
      )}
      <AttachmentList workspace={workspace} files={note.attachments} onRemove={note.can_edit ? setRemoving : undefined} />
      {refusedFiles.length || queue.items.length ? (
        <div className="mt-2 space-y-1.5">
          <RefusedFiles messages={refusedFiles} />
          <UploadList items={queue.items} busy={queue.busy} onRetry={(entry) => void queue.retry(note.id, entry)} onDismiss={queue.dismiss} />
        </div>
      ) : null}
      <p aria-live="polite" className="sr-only">
        {status}
      </p>
      <ConfirmDialog
        open={removing !== null}
        title="Remove this file?"
        confirmLabel="Remove file"
        tone="danger"
        busy={remove.isPending}
        error={remove.isError && !isApiError(remove.error, 404) ? describeError(remove.error) : null}
        onConfirm={() => {
          if (removing) remove.mutate(removing);
        }}
        onCancel={() => {
          remove.reset();
          setRemoving(null);
        }}
      >
        <p className="[overflow-wrap:anywhere]">&ldquo;{removing?.name}&rdquo; will be removed from this note.</p>
      </ConfirmDialog>
    </li>
  );
}

/**
 * Editing a note's text with the version it showed. If it changed meanwhile (409) the typed
 * text stays, the latest version is shown under it, and saving again replaces it.
 */
function NoteEditor({
  workspace,
  opportunityId,
  note,
  onDone,
}: {
  workspace: Workspace;
  opportunityId: string;
  note: Note;
  onDone: (saved: boolean) => void;
}) {
  const sync = useActivityWriteSync(workspace);
  const cache = useDealNotesCache(workspace, opportunityId);
  const fieldId = useId();
  const [text, setText] = useState(note.description);
  const [version, setVersion] = useState(note.version);
  const [conflict, setConflict] = useState<{ kind: "changed"; latest: string } | { kind: "archived" | "unavailable" } | null>(null);
  const save = useMutation({
    mutationFn: () => activitiesApi.update(workspace, note.id, { version, description: text }),
    onSuccess: (saved) => {
      cache.patch(note.id, (current) => ({
        ...current,
        description: saved.description,
        version: saved.version,
        edited_at: saved.edited_at,
        edited_by: saved.edited_by,
      }));
      sync(saved);
      onDone(true);
    },
    onError: async (error) => {
      if (!isApiError(error, 409)) return;
      cache.refresh(note.id);
      const latest = await activitiesApi.get(workspace, note.id).catch(() => null);
      if (!latest) {
        setConflict({ kind: "unavailable" });
        return;
      }
      setVersion(latest.version);
      setConflict(latest.archived_at ? { kind: "archived" } : { kind: "changed", latest: latest.description });
    },
  });
  const [problem, setProblem] = useState<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    if (!text.trim()) {
      setProblem("Write the note first.");
      box.current?.focus();
      return;
    }
    setProblem(null);
    save.mutate();
  };
  const message =
    problem ??
    (save.isError && !isApiError(save.error, 409)
      ? (fieldErrors(save.error).description?.join(" ") ?? describeError(save.error).message)
      : null);

  return (
    <form onSubmit={submit} noValidate className="mt-2 space-y-2">
      {conflict ? (
        <Alert tone="error" title={conflict.kind === "archived" ? "This note was archived meanwhile" : "This note changed meanwhile"}>
          {conflict.kind === "changed"
            ? "Your text is still here, not saved. The latest version is below: save again to replace it, or cancel to keep it."
            : conflict.kind === "archived"
              ? "Your text is still here, not saved."
              : "Your text is still here, not saved. Try saving again in a moment."}
        </Alert>
      ) : null}
      <label htmlFor={fieldId} className="sr-only">
        Edit note
      </label>
      <textarea
        ref={box}
        id={fieldId}
        rows={5}
        maxLength={NOTE_MAX}
        value={text}
        autoFocus
        readOnly={save.isPending}
        aria-busy={save.isPending || undefined}
        onChange={(event) => setText(event.target.value)}
        aria-invalid={message ? true : undefined}
        aria-describedby={message ? `${fieldId}-error` : undefined}
        className={TEXTAREA}
      />
      {conflict?.kind === "changed" ? (
        <figure className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
          <figcaption className="mb-1 text-xs font-medium text-slate-600">Latest version (saved)</figcaption>
          <p className="whitespace-pre-wrap break-words text-sm text-slate-900 [overflow-wrap:anywhere]">{conflict.latest}</p>
        </figure>
      ) : null}
      {message ? (
        <p id={`${fieldId}-error`} role="alert" className="text-xs text-red-600">
          {message}
        </p>
      ) : null}
      <div className="flex justify-end gap-2">
        <Button variant="secondary" size="sm" disabled={save.isPending} onClick={() => onDone(false)}>
          Cancel
        </Button>
        <Button type="submit" size="sm" loading={save.isPending} disabled={conflict?.kind === "archived"}>
          Save
        </Button>
      </div>
    </form>
  );
}
