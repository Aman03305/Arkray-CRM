"use client";

import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";

import type { Lead, LeadOptions } from "@/lib/api/types";
import { hasCapability, type Viewer } from "@/lib/viewer";
import type { Workspace } from "@/lib/workspace";

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
 * and cards show the lead's name.
 */
export function syncAfterLeadWrite(queryClient: QueryClient, workspace: Workspace, lead: Lead): void {
  const key = leadKeys.detail(workspace, lead.id);
  queryClient.setQueryData(key, lead);
  void queryClient.invalidateQueries({
    queryKey: leadKeys.all,
    predicate: (query) => JSON.stringify(query.queryKey) !== JSON.stringify(key),
  });
  void queryClient.invalidateQueries({ queryKey: pipelineKeys.all });
}

export function useLeadWriteSync(workspace: Workspace) {
  const queryClient = useQueryClient();
  return useCallback((lead: Lead) => syncAfterLeadWrite(queryClient, workspace, lead), [queryClient, workspace]);
}

export function statusName(options: LeadOptions | undefined, key: string): string {
  return options?.statuses.find((s) => s.key === key)?.name ?? key;
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
