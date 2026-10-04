"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

import { ActionMenu, type MenuAction } from "@/components/ui/ActionMenu";
import { Button } from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import { PersonName } from "@/features/leads/LeadBits";
import type { ActivityListItem } from "@/lib/api/types";
import { formatDateTime, formatRelative } from "@/lib/format";
import { activityHref, type Workspace } from "@/lib/workspace";

import type { LifecycleAction } from "./api";
import { activityName, LeadLink, OpportunityLink, scheduleOf, StatusBadge, TypeLabel, When } from "./ActivityBits";
import { canComplete, useClock } from "./clock";

interface ActivityTableProps {
  activities: readonly ActivityListItem[] | undefined;
  loading: boolean;
  workspace: Workspace;
  showOwner: boolean;
  canWrite: boolean;
  busyId: string | null;
  onAction: (action: LifecycleAction, activity: ActivityListItem) => void;
}

const TH = "whitespace-nowrap px-4 py-2.5 text-left text-xs font-medium uppercase tracking-wide text-slate-500";
const TD = "px-4 py-3 text-slate-600";

function actionsFor(activity: ActivityListItem, canWrite: boolean, onAction: ActivityTableProps["onAction"], open: () => void): MenuAction[] {
  const actions: MenuAction[] = [{ key: "open", label: "Open", onSelect: open }];
  if (!canWrite) return actions;
  if (activity.archived_at) {
    actions.push({ key: "restore", label: "Restore", onSelect: () => onAction("restore", activity) });
    return actions;
  }
  const current = activity.status === "open" || activity.status === "scheduled";
  if (activity.type !== "note") {
    if (current) actions.push({ key: "cancel", label: "Cancel", onSelect: () => onAction("cancel", activity) });
    else actions.push({ key: "reopen", label: "Reopen", onSelect: () => onAction("reopen", activity) });
  }
  actions.push({ key: "archive", label: "Archive", tone: "danger", onSelect: () => onAction("archive", activity) });
  return actions;
}

function Subject({ activity, workspace }: { activity: ActivityListItem; workspace: Workspace }) {
  const text = activity.type === "note" ? `${activity.preview}${activity.preview_truncated ? "…" : ""}` : activity.title;
  return (
    <Link href={activityHref(workspace, activity.id)} className="line-clamp-2 break-words font-medium text-slate-900 hover:text-brand-700 hover:underline">
      {text}
    </Link>
  );
}

/**
 * Activities as a table on tablets and desktops (columns appear as space allows) and as
 * cards on phones. Type and status are written out; colour only reinforces them. Complete
 * is a real button on each row (keyboard and touch); the rest is in the row's menu.
 */
export function ActivityTable({ activities, loading, workspace, showOwner, canWrite, busyId, onAction }: ActivityTableProps) {
  const router = useRouter();
  const clock = useClock();

  if (loading) {
    return (
      <div aria-busy="true" className="space-y-2 rounded-lg border border-slate-200 bg-white p-4">
        {Array.from({ length: 6 }, (_, i) => (
          <Skeleton key={i} className="block h-5 w-full" />
        ))}
        <span className="sr-only">Loading activities</span>
      </div>
    );
  }

  const complete = (activity: ActivityListItem) =>
    canWrite && canComplete(activity, clock) ? (
      <Button
        variant="secondary"
        size="sm"
        aria-label={`Complete ${activity.title}`}
        loading={busyId === activity.id}
        onClick={() => onAction("complete", activity)}
      >
        Complete
      </Button>
    ) : null;
  const menu = (activity: ActivityListItem) => (
    <ActionMenu
      label={`Actions for ${activityName(activity)}`}
      actions={actionsFor(activity, canWrite, onAction, () => router.push(activityHref(workspace, activity.id)))}
      // An action on this row is running: no second one with the version it started from.
      disabled={busyId === activity.id}
    />
  );

  return (
    <>
      {/* Positioned, so its screen-reader-only labels (absolutely positioned) scroll and clip
          with the table: otherwise "Actions" sat beyond the scroll container and made the
          whole page scroll sideways at desktop widths (found in the Phase 7 walkthrough). */}
      <div className="relative hidden overflow-x-auto rounded-lg border border-slate-200 bg-white md:block">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <caption className="sr-only">Activities</caption>
          <thead className="bg-slate-50">
            <tr>
              <th scope="col" className={TH}>Type</th>
              <th scope="col" className={TH}>Subject</th>
              <th scope="col" className={`${TH} hidden lg:table-cell`}>Related to</th>
              {showOwner ? <th scope="col" className={`${TH} hidden lg:table-cell`}>Owner</th> : null}
              <th scope="col" className={TH}>Status</th>
              <th scope="col" className={TH}>Due / start</th>
              <th scope="col" className={`${TH} hidden xl:table-cell`}>Updated</th>
              <th scope="col" className={`${TH} text-right`}>
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {activities?.map((activity) => (
              <tr key={activity.id} className="align-top hover:bg-slate-50/60">
                <td className={`${TD} whitespace-nowrap`}>
                  <TypeLabel type={activity.type} />
                </td>
                <td className="max-w-80 px-4 py-3">
                  <Subject activity={activity} workspace={workspace} />
                </td>
                <td className={`${TD} hidden max-w-60 lg:table-cell`}>
                  <span className="block truncate">
                    <LeadLink workspace={workspace} lead={activity.lead} />
                  </span>
                  {activity.opportunity ? (
                    <span className="block truncate text-xs">
                      <OpportunityLink workspace={workspace} opportunity={activity.opportunity} />
                    </span>
                  ) : null}
                </td>
                {showOwner ? (
                  <td className={`${TD} hidden whitespace-nowrap lg:table-cell`}>
                    <PersonName person={activity.owner} />
                  </td>
                ) : null}
                <td className="px-4 py-3">
                  <StatusBadge status={activity.status} overdue={activity.is_overdue} type={activity.type} />
                </td>
                <td className={`${TD} whitespace-nowrap`}>
                  {activity.type === "note" ? <span className="text-slate-400">—</span> : <When iso={scheduleOf(activity)} empty="No due date" />}
                </td>
                <td className={`${TD} hidden whitespace-nowrap xl:table-cell`}>
                  <time dateTime={activity.updated_at} title={formatDateTime(activity.updated_at)}>
                    {formatRelative(activity.updated_at)}
                  </time>
                </td>
                <td className="whitespace-nowrap px-4 py-3 text-right">
                  <div className="inline-flex items-center gap-1">
                    {complete(activity)}
                    {menu(activity)}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ul aria-label="Activities" className="space-y-2 md:hidden">
        {activities?.map((activity) => (
          <li key={activity.id} className="rounded-lg border border-slate-200 bg-white p-4">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 space-y-1">
                <TypeLabel type={activity.type} className="text-xs" />
                <Subject activity={activity} workspace={workspace} />
              </div>
              {menu(activity)}
            </div>
            <dl className="mt-3 grid grid-cols-1 gap-y-1 text-xs text-slate-600">
              <dt className="sr-only">Related to</dt>
              <dd className="truncate">
                <LeadLink workspace={workspace} lead={activity.lead} />
                {activity.opportunity ? (
                  <>
                    {" · "}
                    <OpportunityLink workspace={workspace} opportunity={activity.opportunity} />
                  </>
                ) : null}
              </dd>
              {activity.status ? (
                <>
                  <dt className="sr-only">Status</dt>
                  <dd>
                    <StatusBadge status={activity.status} overdue={activity.is_overdue} type={activity.type} />
                  </dd>
                </>
              ) : null}
              {activity.type !== "note" ? (
                <>
                  <dt className="sr-only">Due or start</dt>
                  <dd>
                    <When iso={scheduleOf(activity)} empty="No due date" />
                  </dd>
                </>
              ) : (
                <>
                  <dt className="sr-only">Written</dt>
                  <dd>
                    <PersonName person={activity.created_by} /> · {formatRelative(activity.created_at)}
                  </dd>
                </>
              )}
              {showOwner ? (
                <>
                  <dt className="sr-only">Owner</dt>
                  <dd>
                    <PersonName person={activity.owner} />
                  </dd>
                </>
              ) : null}
            </dl>
            {complete(activity) ? <div className="mt-3">{complete(activity)}</div> : null}
          </li>
        ))}
      </ul>
    </>
  );
}
