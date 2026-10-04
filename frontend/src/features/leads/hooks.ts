"use client";

import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";

import type { Lead } from "@/lib/api/types";
import { hasCapability, type Viewer } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";
import type { Workspace } from "@/lib/workspace";

import { activityKeys, timelineKeys } from "@/features/activities/api";
import { pipelineKeys } from "@/features/pipeline/api";

import { leadKeys, leadsApi } from "./api";

/** Statuses, sources, ratings and countries. Configuration: cached for the session. */
export function useLeadOptions() {
  return useQuery({ queryKey: leadKeys.options, queryFn: leadsApi.options, staleTime: 10 * 60_000 });
}

/**
 * After any lead write: cache the returned lead for this workspace, and mark every other
 * lead query stale (lists, the same lead cached under other workspaces, duplicate checks),
 * so no page shows the lead as it was. The lead just written isn't refetched: its fresh copy
 * is in hand, and after a reassignment out of this workspace it would no longer be found.
 * Pipeline data is marked stale too: a reassignment moves the lead's open opportunities,
 * and cards show the lead's name. So are activities and timelines: a reassignment moves
 * the lead's current work, and every lead change is on its timeline.
 *
 * A reassignment out of the workspace being viewed (`viewerId`: whose "self" workspace it
 * is) leaves the lead's own timeline and open work under that workspace alone: the page
 * still showing them would refetch them where the lead no longer is (a 404 flashing up as
 * an error while the page moves away). The lead page drops them once it is gone. If the
 * write only finishes after its page was left (Back pressed during the request), nothing
 * is showing them: they are dropped now, never cached as this workspace's fresh copy of a
 * lead that lives elsewhere (Phase 6 review).
 */
export function syncAfterLeadWrite(queryClient: QueryClient, workspace: Workspace, lead: Lead, viewerId?: string): void {
  const key = leadKeys.detail(workspace, lead.id);
  const moved = movedOutOf(workspace, lead, viewerId);
  const shown = (queryClient.getQueryCache().find({ queryKey: key, exact: true })?.getObserversCount() ?? 0) > 0;
  if (moved && !shown) {
    const gone = [JSON.stringify(key), ...leftBehind(workspace, lead.id)];
    queryClient.removeQueries({ predicate: (query) => gone.includes(JSON.stringify(query.queryKey)) });
  } else {
    queryClient.setQueryData(key, lead);
  }
  void queryClient.invalidateQueries({
    queryKey: leadKeys.all,
    predicate: (query) => JSON.stringify(query.queryKey) !== JSON.stringify(key),
  });
  const left = moved ? leftBehind(workspace, lead.id) : [];
  const keep = (queryKey: readonly unknown[]) => !left.includes(JSON.stringify(queryKey));
  void queryClient.invalidateQueries({ queryKey: pipelineKeys.all });
  void queryClient.invalidateQueries({ queryKey: activityKeys.all, predicate: (query) => keep(query.queryKey) });
  void queryClient.invalidateQueries({ queryKey: timelineKeys.all, predicate: (query) => keep(query.queryKey) });
}

/** Whether `lead` now belongs to someone other than the owner of the workspace viewed. */
export function movedOutOf(workspace: Workspace, lead: Pick<Lead, "owner">, viewerId?: string): boolean {
  const owner = workspace.kind === "user" ? workspace.userId : workspace.kind === "self" ? viewerId : undefined;
  return owner !== undefined && lead.owner.id !== owner;
}

/** The lead page's own activity queries under `workspace` (serialised query keys). */
export function leftBehind(workspace: Workspace, leadId: string): string[] {
  return [
    JSON.stringify(timelineKeys.subject(workspace, { kind: "lead", id: leadId })),
    JSON.stringify(activityKeys.current(workspace, { lead: leadId })),
  ];
}

export function useLeadWriteSync(workspace: Workspace) {
  const queryClient = useQueryClient();
  const viewerId = useViewer()?.id;
  return useCallback((lead: Lead) => syncAfterLeadWrite(queryClient, workspace, lead, viewerId), [queryClient, workspace, viewerId]);
}

export interface LeadPermissions {
  /** Edit, change status, archive and restore leads in this workspace. */
  canWrite: boolean;
  /** Reassign leads to another user. */
  canAssign: boolean;
  /** Create leads here (someone else's workspace needs assigning rights too). */
  canCreate: boolean;
  /** The organisation-wide workspace: a new lead's owner must be chosen. */
  choosesOwner: boolean;
}

/**
 * What the UI offers in this workspace, from the viewer's capabilities (mirroring
 * identity.workspaces.authorize_write on the server). Presentation only: the API decides.
 */
export function leadPermissions(viewer: Viewer | null, workspace: Workspace): LeadPermissions {
  const own = workspace.kind === "self";
  const canWrite = hasCapability(viewer, own ? "crm.access_own" : "crm.manage_any");
  const canAssign = hasCapability(viewer, "crm.assign_any");
  return {
    canWrite,
    canAssign,
    canCreate: canWrite && (own || canAssign),
    choosesOwner: workspace.kind === "organization",
  };
}

export function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

const COUNTRY_NAMES = (() => {
  try {
    return new Intl.DisplayNames(["en"], { type: "region" });
  } catch {
    return null;
  }
})();

export function countryName(code: string): string {
  if (!code) return "";
  try {
    return COUNTRY_NAMES?.of(code) ?? code;
  } catch {
    return code;
  }
}
