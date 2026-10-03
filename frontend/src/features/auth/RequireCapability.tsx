"use client";

import type { ReactNode } from "react";

import { NotFoundView } from "@/components/ui/NotFoundView";
import { Skeleton } from "@/components/ui/Skeleton";
import { type Capability, hasCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";

/**
 * Shows `children` only to viewers holding `capability`; everyone else sees the standard
 * "not found" page. Presentation only: the API independently refuses (403/404).
 */
export function RequireCapability({ capability, children }: { capability: Capability; children: ReactNode }) {
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
  return hasCapability(viewer, capability) ? <>{children}</> : <NotFoundView />;
}
