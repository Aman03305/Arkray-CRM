"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import Link from "next/link";
import { type ReactNode, useEffect, useId, useRef } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import { PersonName } from "@/components/ui/PersonName";
import { describeError, isApiError } from "@/lib/api/errors";
import { cursorOf } from "@/lib/api/pagination";
import type { TimelineEntry } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { activityHref, opportunityHref, type Workspace } from "@/lib/workspace";

import { activitiesApi, timelineKeys, type TimelineSubject } from "./api";
import { TypeIcon } from "./ActivityBits";

/**
 * An opportunity's history, newest first, 20 at a time ("Show older" adds the next page).
 * Each entry was written when it happened; activities are shown only while this workspace
 * may see them (the server decides), and a note appears as a bounded preview with a link to
 * the whole note. (Its customer record's own events, the old Leads timeline, are on no
 * opportunity's timeline: ADR-0027.)
 */
export function Timeline({ workspace, subject }: { workspace: Workspace; subject: TimelineSubject }) {
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
      if (type !== "task" && type !== "meeting") {
        // A customer record's own events (lead.*) are on no opportunity's timeline; should one
        // ever arrive, it is described plainly, never as a task or a meeting.
        summary = "Customer record updated";
        break;
      }
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
