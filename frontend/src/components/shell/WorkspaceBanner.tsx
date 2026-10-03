import { ArrowLeft, Eye } from "lucide-react";
import Link from "next/link";

import { Skeleton } from "@/components/ui/Skeleton";

/**
 * Persistent reminder that an admin is viewing someone else's CRM. Rendered by the
 * /admin/users/[userId] layout, so it stays visible on every page of that workspace.
 */
export function WorkspaceBanner({ subjectName, note }: { subjectName?: string; note?: string }) {
  return (
    <section
      aria-label="Workspace context"
      className="mb-6 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3"
    >
      <p className="flex items-center gap-2 text-sm text-amber-900">
        <Eye aria-hidden="true" className="size-4 shrink-0" />
        <span>
          Viewing CRM for:{" "}
          {subjectName ? (
            <strong className="font-semibold">{subjectName}</strong>
          ) : (
            <>
              <Skeleton className="h-3.5 w-32 bg-amber-200 align-middle" />
              <span className="sr-only">Loading user name</span>
            </>
          )}
        </span>
        {note ? <span className="text-xs text-amber-800">({note})</span> : null}
      </p>
      <Link
        href="/admin/users"
        className="inline-flex items-center gap-1 text-sm font-medium text-amber-900 hover:underline"
      >
        <ArrowLeft aria-hidden="true" className="size-4" />
        Back to Users
      </Link>
    </section>
  );
}
