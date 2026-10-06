"use client";

import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import Link from "next/link";
import { useEffect, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import { TALLER_HIT } from "@/components/ui/targets";
import { useSingleFlight } from "@/components/ui/useSingleFlight";
import { describeError, isApiError } from "@/lib/api/errors";
import type { ActivityListItem } from "@/lib/api/types";
import { activityHref, type Workspace, workspaceApiSegment, workspaceHref } from "@/lib/workspace";

import { activitiesApi, activityKeys, type CurrentWorkTarget } from "./api";
import { TypeLabel, When } from "./ActivityBits";
import { ActivityFormDialog } from "./ActivityFormDialog";
import { canComplete, useClock } from "./clock";
import type { FormKind } from "./draft";
import { useActivityAction } from "./hooks";
import { presetActivityList } from "./list-state";

/**
 * An opportunity's open tasks and scheduled meetings, soonest first, with Complete at hand
 * and + Task / + Meeting for this opportunity. Only this workspace's work is listed (the
 * server scopes it).
 */
export function CurrentWork({
  workspace,
  target,
  label,
  canWrite,
}: {
  workspace: Workspace;
  target: CurrentWorkTarget;
  /** What it is about, shown in the forms ("Analyser upgrade"). */
  label: string;
  canWrite: boolean;
}) {
  const headingId = useId();
  const [form, setForm] = useState<FormKind | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "error"; text: string; requestId?: string | null } | null>(null);
  // After Complete the row (and the button that had focus) goes: focus moves to the message.
  const noticeRegion = useRef<HTMLDivElement>(null);
  const focusNotice = useRef(false);
  useEffect(() => {
    if (focusNotice.current && notice) {
      focusNotice.current = false;
      noticeRegion.current?.focus();
    }
  }, [notice]);
  const clock = useClock();
  const work = useQuery({
    queryKey: activityKeys.current(workspace, target),
    queryFn: () => activitiesApi.current(workspace, target),
  });
  const action = useActivityAction(workspace);
  const once = useSingleFlight();
  const rows = work.data?.results;

  // Once per row at a time: a second click in the same moment would carry the same version
  // and report "changed by someone else" about the first.
  const complete = (row: ActivityListItem) =>
    once(() => {
      setNotice(null);
      return action.mutateAsync(
        { activity: row, action: "complete" },
        {
          onSuccess: () => {
            focusNotice.current = true;
            setNotice({ tone: "success", text: `${row.title} completed.` });
          },
          onError: (error) => {
            focusNotice.current = true;
            setNotice({
              tone: "error",
              text: isApiError(error, 409)
                ? `${row.title} was changed by someone else a moment ago. The list has been refreshed; try again.`
                : describeError(error).message,
              requestId: describeError(error).requestId,
            });
          },
        },
      );
    }, row.id);

  return (
    <section aria-labelledby={headingId} className="rounded-lg border border-slate-200 bg-white p-5">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 id={headingId} className="text-sm font-semibold text-slate-900">
          Open work
        </h2>
        {canWrite ? (
          <div className="flex gap-1">
            <Button variant="ghost" size="sm" aria-label="New task" icon={<Plus aria-hidden="true" className="size-4" />} onClick={() => setForm("task")}>
              Task
            </Button>
            <Button variant="ghost" size="sm" aria-label="New meeting" icon={<Plus aria-hidden="true" className="size-4" />} onClick={() => setForm("meeting")}>
              Meeting
            </Button>
          </div>
        ) : null}
      </div>
      <div ref={noticeRegion} tabIndex={-1} aria-live="polite" className="empty:hidden focus:outline-none">
        {notice ? (
          <div className="mb-3">
            <Alert tone={notice.tone} requestId={notice.requestId}>
              {notice.text}
            </Alert>
          </div>
        ) : null}
      </div>
      {work.isError ? (
        <p className="text-sm text-red-700">Open work couldn&apos;t be loaded. {describeError(work.error).message}</p>
      ) : !rows ? (
        <Skeleton className="h-12 w-full" />
      ) : rows.length === 0 ? (
        <p className="text-sm text-slate-500">No open tasks or scheduled meetings.</p>
      ) : (
        <ul className="divide-y divide-slate-100">
          {rows.map((row) => {
            return (
              <li key={row.id} className="flex items-start justify-between gap-3 py-2.5 first:pt-0 last:pb-0">
                <div className="min-w-0">
                  <Link href={activityHref(workspace, row.id)} className={`block truncate text-sm font-medium text-brand-700 hover:underline ${TALLER_HIT}`}>
                    {row.title}
                  </Link>
                  <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-slate-600">
                    <TypeLabel type={row.type} />
                    <When iso={row.type === "task" ? row.due_at : row.starts_at} empty="No due date" />
                    {row.is_overdue ? (
                      <span className="font-medium text-red-700">{row.type === "meeting" ? "Awaiting outcome" : "Overdue"}</span>
                    ) : null}
                  </div>
                </div>
                {canWrite && canComplete(row, clock) ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    aria-label={`Complete ${row.title}`}
                    loading={action.isPending && action.variables?.activity.id === row.id}
                    onClick={() => complete(row)}
                  >
                    Complete
                  </Button>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {work.data?.next ? (
        <p className="mt-3 text-xs">
          <Link
            href={workspaceHref(workspace, "activities")}
            // The rest of *this* record's current work, not the whole workspace's newest
            // activities (whole-software audit). onNavigate: same-tab navigations only.
            onNavigate={() =>
              presetActivityList(workspaceApiSegment(workspace), {
                status: "current",
                ordering: "scheduled",
                opportunity: target.opportunity,
                opportunityLabel: label,
              })
            }
            className="font-medium text-brand-700 hover:underline"
          >
            More in Activities
          </Link>
        </p>
      ) : null}
      {form ? (
        <ActivityFormDialog
          workspace={workspace}
          kind={form}
          mode={{ kind: "create", opportunity: { id: target.opportunity, label } }}
          onClose={() => setForm(null)}
          onSaved={(saved) => {
            setForm(null);
            setNotice({ tone: "success", text: saved.type === "task" ? "Task created." : "Meeting scheduled." });
          }}
        />
      ) : null}
    </section>
  );
}
