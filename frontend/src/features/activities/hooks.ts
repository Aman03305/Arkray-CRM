"use client";

import { type QueryClient, useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { leadKeys } from "@/features/leads/api";
import type { Activity, ActivityListItem, ActivityPage } from "@/lib/api/types";
import { hasCapability, type Viewer } from "@/lib/viewer";
import type { Workspace } from "@/lib/workspace";

import { activitiesApi, activityKeys, type LifecycleAction, timelineKeys } from "./api";

export interface ActivityPermissions {
  /** Create, edit, complete, cancel, reopen, archive and restore activities here. */
  canWrite: boolean;
}

/**
 * What the UI offers in this workspace (mirroring identity.workspaces.authorize_write on
 * the server). Nobody picks an activity's owner (current work follows the lead), so
 * creating needs no assigning rights. Presentation only: the API decides.
 */
export function activityPermissions(viewer: Viewer | null, workspace: Workspace): ActivityPermissions {
  return { canWrite: hasCapability(viewer, workspace.kind === "self" ? "crm.access_own" : "crm.manage_any") };
}

/** A note's text is its author's words: only the author edits it (others may archive it). */
export function canEditNote(viewer: Viewer | null, activity: Pick<Activity, "type" | "created_by">): boolean {
  return activity.type !== "note" || activity.created_by.id === viewer?.id;
}

/** Characters of a description in a list row's preview (the server's PREVIEW_LENGTH). */
const PREVIEW_LENGTH = 240;

/**
 * A cached list row brought up to date with the activity a write returned: its state and
 * version (the next action from that row must carry the new version, not a stale one that
 * the server would refuse with a 409). The row keeps its own lead and opportunity
 * references: they are what the workspace it was listed in may see.
 */
function updatedRow(row: ActivityListItem, activity: Activity): ActivityListItem {
  const characters = Array.from(activity.description); // code points, as the server counts
  return {
    ...row,
    title: activity.title,
    preview: characters.slice(0, PREVIEW_LENGTH).join(""),
    preview_truncated: characters.length > PREVIEW_LENGTH,
    status: activity.status,
    priority: activity.priority,
    due_at: activity.due_at,
    starts_at: activity.starts_at,
    ends_at: activity.ends_at,
    is_overdue: activity.is_overdue,
    completable: activity.completable,
    completed_at: activity.completed_at,
    cancelled_at: activity.cancelled_at,
    archived_at: activity.archived_at,
    version: activity.version,
    updated_at: activity.updated_at,
  };
}

/**
 * After any activity write: cache the returned activity for this workspace, update its row
 * wherever this workspace's lists and current-work cards hold it (so a list shown again
 * before its reload offers the right actions with the right version), and mark every
 * other activity query (lists, counts, current work) and every timeline stale, so no page
 * shows it as it was. Completing a meeting records a contact on the lead (a new version
 * and last-contacted time), so the lead's queries are marked stale too.
 */
export function syncAfterActivityWrite(queryClient: QueryClient, workspace: Workspace, activity?: Activity): void {
  const key = activity ? activityKeys.detail(workspace, activity.id) : null;
  if (activity && key) {
    queryClient.setQueryData(key, activity);
    const update = (page: ActivityPage | undefined) =>
      page?.results.some((row) => row.id === activity.id)
        ? { ...page, results: page.results.map((row) => (row.id === activity.id ? updatedRow(row, activity) : row)) }
        : page;
    queryClient.setQueriesData<ActivityPage>({ queryKey: activityKeys.lists(workspace) }, update);
    queryClient.setQueriesData<ActivityPage>({ queryKey: activityKeys.currentWork(workspace) }, update);
  }
  void queryClient.invalidateQueries({
    queryKey: activityKeys.all,
    predicate: (query) => key === null || JSON.stringify(query.queryKey) !== JSON.stringify(key),
  });
  void queryClient.invalidateQueries({ queryKey: timelineKeys.all });
  if (!activity || (activity.type === "meeting" && activity.status === "completed")) {
    void queryClient.invalidateQueries({ queryKey: leadKeys.all });
  }
}

export function useActivityWriteSync(workspace: Workspace) {
  const queryClient = useQueryClient();
  return useCallback((activity?: Activity) => syncAfterActivityWrite(queryClient, workspace, activity), [queryClient, workspace]);
}

/** Complete / cancel / reopen / archive / restore, with the version the caller shows. */
export function useActivityAction(workspace: Workspace) {
  const sync = useActivityWriteSync(workspace);
  return useMutation({
    mutationFn: ({ activity, action }: { activity: Pick<ActivityListItem, "id" | "version">; action: LifecycleAction }) =>
      activitiesApi.act(workspace, activity.id, action, activity.version),
    onSuccess: (activity) => sync(activity),
    // A 409 (changed meanwhile) or 404 (gone from this workspace): reload what is shown.
    onError: () => sync(),
  });
}
