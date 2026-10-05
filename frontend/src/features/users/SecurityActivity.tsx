"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { type ReactNode, useId } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import { describeError } from "@/lib/api/errors";
import { formatDateTime, formatRelative } from "@/lib/format";

import { cursorOf, SECURITY_EVENTS_QUERY_KEY, securityApi } from "./api";
import { describeSecurityEvent } from "./security-events";

/**
 * Recent sign-in and account events (password changes, users created or deactivated,
 * support sessions), one line each, newest first, for viewers who may read the security log.
 */
export function SecurityActivity() {
  const headingId = useId();
  const events = useInfiniteQuery({
    queryKey: SECURITY_EVENTS_QUERY_KEY,
    queryFn: ({ pageParam }) => securityApi.events(pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => cursorOf(last.next),
  });
  const items = events.data?.pages.flatMap((page) => page.results) ?? [];
  const error = events.isError ? describeError(events.error) : null;

  let body: ReactNode;
  if (error && items.length === 0) {
    body = (
      <Alert
        tone="error"
        requestId={error.requestId}
        action={
          <Button variant="secondary" size="sm" onClick={() => void events.refetch()} loading={events.isFetching}>
            Try again
          </Button>
        }
      >
        {error.message}
      </Alert>
    );
  } else if (events.isPending) {
    body = (
      <ul aria-busy="true" className="space-y-3 py-1">
        {Array.from({ length: 3 }, (_, i) => (
          <li key={i}>
            <Skeleton className="h-4 w-64 max-w-full" />
          </li>
        ))}
        <li className="sr-only">Loading</li>
      </ul>
    );
  } else if (items.length === 0) {
    body = <p className="py-1 text-sm text-slate-500">No security activity yet.</p>;
  } else {
    body = (
      <>
        <ul className="divide-y divide-slate-100">
          {items.map((event) => (
            <li key={event.id} className="py-2 text-sm text-slate-800 [overflow-wrap:anywhere]">
              {describeSecurityEvent(event)}
              {event.in_support_session ? <span className="text-slate-500"> (in a support session)</span> : null}
              <span aria-hidden="true" className="text-slate-400">
                {" · "}
              </span>
              <time dateTime={event.occurred_at} title={formatDateTime(event.occurred_at)} className="text-slate-500">
                {formatRelative(event.occurred_at)}
              </time>
            </li>
          ))}
        </ul>
        {error ? (
          <div className="mt-2">
            <Alert tone="error" requestId={error.requestId}>
              {error.message}
            </Alert>
          </div>
        ) : null}
        {events.hasNextPage ? (
          <Button
            variant="ghost"
            size="sm"
            className="mt-1"
            onClick={() => void events.fetchNextPage()}
            loading={events.isFetchingNextPage}
          >
            Show more
          </Button>
        ) : null}
      </>
    );
  }

  return (
    <section aria-labelledby={headingId} className="mt-6 rounded-lg border border-slate-200 bg-white px-5 py-4">
      <h2 id={headingId} className="mb-2 flex items-center gap-2 text-sm font-semibold text-slate-900">
        <ShieldCheck aria-hidden="true" className="size-4 text-slate-500" />
        Security activity
      </h2>
      {body}
    </section>
  );
}
