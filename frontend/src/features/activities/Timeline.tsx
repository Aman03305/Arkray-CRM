"use client";

import { useInfiniteQuery, useMutation } from "@tanstack/react-query";
import Link from "next/link";
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import { cursorOf } from "@/features/leads/api";
import { PersonName } from "@/features/leads/LeadBits";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { ActivityCreateRequest, TimelineEntry, UserRef } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { randomUuid } from "@/lib/random";
import { activityHref, opportunityHref, type Workspace } from "@/lib/workspace";

import { activitiesApi, timelineKeys, type TimelineSubject } from "./api";
import { TypeIcon } from "./ActivityBits";
import { useActivityWriteSync } from "./hooks";

const NOTE_MAX = 10_000;

/**
 * A lead's or an opportunity's history, newest first, 20 at a time ("Show older" adds the
 * next page). Each entry was written when it happened; activities and opportunities are
 * shown only while this workspace may see them (the server decides), and a note appears as
 * a bounded preview with a link to the whole note.
 */
export function Timeline({
  workspace,
  subject,
  composer,
}: {
  workspace: Workspace;
  subject: TimelineSubject;
  /** The quick note form above the history, when the viewer may add notes here. */
  composer?: ReactNode;
}) {
  const headingId = useId();
  const timeline = useInfiniteQuery({
    queryKey: timelineKeys.subject(workspace, subject),
    queryFn: ({ pageParam }) => activitiesApi.timeline(workspace, subject, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => cursorOf(last.next),
  });
  const entries = timeline.data?.pages.flatMap((page) => page.results) ?? [];
  // Never leave entries on screen that the server now refuses (moved away, no access).
  const refused = isApiError(timeline.error, 404) || isApiError(timeline.error, 403);
  // "Show older": focus moves to the first entry it added (the button itself may be gone).
  const list = useRef<HTMLOListElement>(null);
  const focusFrom = useRef<number | null>(null);
  useEffect(() => {
    if (focusFrom.current === null || timeline.isFetchingNextPage) return;
    (list.current?.children[focusFrom.current] as HTMLElement | undefined)?.focus();
    focusFrom.current = null;
  }, [timeline.isFetchingNextPage, entries.length]);
  const showOlder = () => {
    focusFrom.current = entries.length;
    void timeline.fetchNextPage();
  };

  return (
    <section aria-labelledby={headingId} className="rounded-lg border border-slate-200 bg-white p-5">
      <h2 id={headingId} className="mb-3 text-sm font-semibold text-slate-900">
        Timeline
      </h2>
      {composer}
      {timeline.isError && (refused || entries.length === 0) ? (
        <Alert
          tone="error"
          requestId={describeError(timeline.error).requestId}
          action={
            refused ? null : (
              <Button variant="secondary" size="sm" onClick={() => void timeline.refetch()} loading={timeline.isFetching}>
                Try again
              </Button>
            )
          }
        >
          The timeline couldn&apos;t be loaded. {describeError(timeline.error).message}
        </Alert>
      ) : timeline.isPending ? (
        <div aria-busy="true" className="space-y-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <span className="sr-only">Loading timeline</span>
        </div>
      ) : entries.length === 0 ? (
        <p className="text-sm text-slate-500">Nothing has happened here yet.</p>
      ) : (
        <>
          {timeline.isError ? (
            <p role="status" className="mb-2 text-xs text-red-700">
              The latest entries couldn&apos;t be loaded; showing what was loaded before.
            </p>
          ) : null}
          <ol ref={list} aria-labelledby={headingId} className="space-y-4">
            {entries.map((entry) => (
              <TimelineItem key={entry.id} entry={entry} workspace={workspace} />
            ))}
          </ol>
          {timeline.hasNextPage ? (
            <div className="mt-4 flex justify-center">
              <Button variant="secondary" size="sm" onClick={showOlder} loading={timeline.isFetchingNextPage}>
                Show older
              </Button>
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

function person(details: TimelineEntry["details"], key: string): UserRef | null {
  const value = details[key];
  if (!value || typeof value !== "object") return null;
  const candidate = value as Partial<UserRef>;
  return typeof candidate.id === "string" && typeof candidate.full_name === "string"
    ? { id: candidate.id, full_name: candidate.full_name, is_active: candidate.is_active !== false }
    : null;
}

function text(details: TimelineEntry["details"], key: string): string {
  const value = details[key];
  return typeof value === "string" ? value : "";
}

function Strong({ children }: { children: ReactNode }) {
  return <strong className="font-medium text-slate-900">{children}</strong>;
}

function TimelineItem({ entry, workspace }: { entry: TimelineEntry; workspace: Workspace }) {
  const { details, activity, opportunity } = entry;
  const opportunityName = opportunity ? (
    opportunity.restricted || !opportunity.id ? (
      <span className="italic text-slate-500">an opportunity in another workspace</span>
    ) : (
      <Link href={opportunityHref(workspace, opportunity.id)} className="font-medium text-brand-700 hover:underline">
        {opportunity.title}
      </Link>
    )
  ) : null;
  const activityName = activity ? (
    <Link href={activityHref(workspace, activity.id)} className="font-medium text-brand-700 hover:underline">
      {activity.title || "Note"}
    </Link>
  ) : null;

  let summary: ReactNode;
  switch (entry.kind) {
    case "lead.created": {
      const owner = person(details, "owner");
      summary = (
        <>
          Lead created{owner && owner.id !== entry.actor?.id ? <> for <Strong><PersonName person={owner} /></Strong></> : null}
          {text(details, "status_name") ? <> as <Strong>{text(details, "status_name")}</Strong></> : null}
        </>
      );
      break;
    }
    case "lead.status_changed":
      summary = (
        <>
          Status changed from <Strong>{text(details, "from_name")}</Strong> to <Strong>{text(details, "to_name")}</Strong>
        </>
      );
      break;
    case "lead.reassigned": {
      const from = person(details, "from_owner");
      const to = person(details, "to_owner");
      summary = (
        <>
          Reassigned{from ? <> from <Strong><PersonName person={from} /></Strong></> : null}
          {to ? <> to <Strong><PersonName person={to} /></Strong></> : null}
        </>
      );
      break;
    }
    case "lead.archived":
      summary = "Lead archived";
      break;
    case "lead.restored":
      summary = "Lead restored";
      break;
    case "opportunity.created":
      summary = (
        <>
          {details.via_conversion === true ? "Converted: opportunity " : "Opportunity "}
          {opportunityName} created in <Strong>{text(details, "stage")}</Strong>
        </>
      );
      break;
    case "opportunity.stage_changed":
      summary = (
        <>
          {opportunityName} moved from <Strong>{text(details, "from_stage")}</Strong> to <Strong>{text(details, "to_stage")}</Strong>
        </>
      );
      break;
    case "opportunity.won":
      summary = <>{opportunityName} won</>;
      break;
    case "opportunity.lost":
      summary = <>{opportunityName} lost</>;
      break;
    case "opportunity.reopened":
      summary = (
        <>
          {opportunityName} reopened in <Strong>{text(details, "to_stage")}</Strong>
        </>
      );
      break;
    case "meeting.scheduled":
      summary = (
        <>
          Meeting scheduled: {activityName}
          {text(details, "starts_at") ? <> for <Strong>{formatDateTime(text(details, "starts_at"))}</Strong></> : null}
        </>
      );
      break;
    case "meeting.rescheduled":
      summary = (
        <>
          Meeting rescheduled: {activityName}
          {text(details, "to_starts_at") ? <> to <Strong>{formatDateTime(text(details, "to_starts_at"))}</Strong></> : null}
        </>
      );
      break;
    case "note.added":
      summary = "Note added";
      break;
    default: {
      // task.created / completed / cancelled / reopened, meeting.completed / cancelled / reopened
      const [type, verb] = entry.kind.split(".");
      const label = type === "task" ? "Task" : "Meeting";
      summary = (
        <>
          {label} {verb}: {activityName}
        </>
      );
    }
  }

  return (
    <li tabIndex={-1} className="border-l-2 border-slate-200 pl-3 text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-600">
      <p className="text-slate-700">
        {activity ? <TypeIcon type={activity.type} /> : null}
        {summary}
        {activity && opportunity && entry.kind !== "note.added" ? <span className="text-slate-500"> · {opportunityName}</span> : null}
      </p>
      {entry.kind === "note.added" && activity ? (
        <div className="mt-1 rounded-md bg-slate-50 px-3 py-2">
          <p className="whitespace-pre-line break-words text-slate-900">
            {activity.preview}
            {activity.preview_truncated ? "…" : null}
          </p>
          <p className="mt-1 text-xs">
            {activity.preview_truncated ? (
              <Link href={activityHref(workspace, activity.id)} className="font-medium text-brand-700 hover:underline">
                Read the whole note
              </Link>
            ) : null}
            {opportunity ? <span className="text-slate-500">{activity.preview_truncated ? " · " : ""}On {opportunityName}</span> : null}
          </p>
        </div>
      ) : null}
      <p className="mt-0.5 text-xs text-slate-500">
        {entry.actor ? <PersonName person={entry.actor} /> : "System"} · <time dateTime={entry.occurred_at}>{formatDateTime(entry.occurred_at)}</time>
      </p>
    </li>
  );
}

/**
 * Type, save: a note in two steps. The note belongs to the lead's owner and is signed by
 * whoever writes it. A save retried with the same text reuses its idempotency key; a
 * failure keeps the text in the box.
 */
export function NoteComposer({ workspace, link, disabled = false }: { workspace: Workspace; link: { lead?: string; opportunity?: string }; disabled?: boolean }) {
  const sync = useActivityWriteSync(workspace);
  const [text, setText] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const fieldId = useId();
  const box = useRef<HTMLTextAreaElement>(null);
  const idempotency = useRef<{ body: string; key: string } | null>(null);
  const save = useMutation({
    mutationFn: (body: ActivityCreateRequest) => {
      const serialised = JSON.stringify(body);
      if (idempotency.current?.body !== serialised) idempotency.current = { body: serialised, key: randomUuid() };
      return activitiesApi.create(workspace, body, idempotency.current.key);
    },
    onSuccess: (note) => {
      sync(note);
      setText("");
      setSaved(true);
      idempotency.current = null;
      box.current?.focus();
    },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    setSaved(false);
    if (!text.trim()) {
      setProblem("Write the note first.");
      box.current?.focus();
      return;
    }
    setProblem(null);
    save.mutate({ type: "note", ...link, description: text });
  };
  const serverProblem = save.isError ? (fieldErrors(save.error).description?.join(" ") ?? describeError(save.error).message) : null;
  const message = problem ?? serverProblem;

  return (
    <form onSubmit={submit} noValidate className="mb-5 space-y-2">
      <label htmlFor={fieldId} className="block text-sm font-medium text-slate-700">
        Add a note
      </label>
      <textarea
        ref={box}
        id={fieldId}
        rows={3}
        maxLength={NOTE_MAX}
        value={text}
        disabled={disabled}
        // Read-only while saving: what is typed then would not be in the saved note (and
        // the box is emptied once it is saved).
        readOnly={save.isPending}
        aria-busy={save.isPending || undefined}
        onChange={(e) => {
          setText(e.target.value);
          setSaved(false);
        }}
        aria-invalid={message ? true : undefined}
        aria-describedby={message ? `${fieldId}-error` : undefined}
        placeholder="What happened? What did they say?"
        className="block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 read-only:bg-slate-50 focus-visible:outline-brand-600 disabled:bg-slate-50"
      />
      {message ? (
        <p id={`${fieldId}-error`} role="alert" className="text-xs text-red-600">
          {message}
        </p>
      ) : null}
      <div className="flex items-center justify-end gap-3">
        <span aria-live="polite" className="text-xs text-emerald-700">
          {saved ? "Note saved." : ""}
        </span>
        <Button type="submit" size="sm" loading={save.isPending} disabled={disabled}>
          Save note
        </Button>
      </div>
    </form>
  );
}
