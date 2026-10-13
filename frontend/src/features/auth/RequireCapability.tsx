"use client";

import type { ReactNode } from "react";

import { NotFoundView } from "@/components/ui/NotFoundView";
import { Skeleton } from "@/components/ui/Skeleton";
import { type Capability, hasAnyCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";

/**
 * Shows `children` only to viewers holding `capability` (or, given several, any one of them:
 * an organisation-wide capability and its team counterpart, ADR-0035); everyone else sees the
 * standard "not found" page. Presentation only: the API independently refuses (403/404).
 */
export function RequireCapability({
  capability,
  children,
}: {
  capability: Capability | readonly Capability[];
  children: ReactNode;
}) {
  const viewer = useViewer();
  if (viewer === null) {
    return (
      <div aria-busy="true" className="space-y-3">
        <Skeleton className="h-6 w-40" />
        <Skeleton className="h-4 w-72" />
        <span className="sr-only">Loading</span>
      </div>
    );
  }
  const required: readonly Capability[] = typeof capability === "string" ? [capability] : capability;
  return hasAnyCapability(viewer, required) ? <>{children}</> : <NotFoundView />;
}
