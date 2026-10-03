"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { NotFoundView } from "@/components/ui/NotFoundView";
import { Skeleton } from "@/components/ui/Skeleton";
import { hasCapability } from "@/lib/viewer";
import { useViewer } from "@/lib/viewer-context";

/**
 * /admin: administrators go to their home, the organisation dashboard (ADR-0010).
 * Everyone else gets the standard "not found" page.
 */
export function AdminEntry() {
  const viewer = useViewer();
  const router = useRouter();
  const allowed = hasCapability(viewer, "users.manage");

  useEffect(() => {
    if (allowed) router.replace("/dashboard");
  }, [allowed, router]);

  if (viewer !== null && !allowed) return <NotFoundView />;
  return (
    <div aria-busy="true">
      <Skeleton className="h-6 w-40" />
      <span className="sr-only">Loading</span>
    </div>
  );
}
