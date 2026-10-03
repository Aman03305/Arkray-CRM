import { ArrowLeft, Eye } from "lucide-react";
import Link from "next/link";

import { Badge } from "@/components/ui/Badge";
import { Skeleton } from "@/components/ui/Skeleton";

type SubjectStatus = "active" | "invited" | "deactivated";

const STATUS: Record<SubjectStatus, { tone: "green" | "amber" | "neutral"; label: string; note?: string }> = {
  active: { tone: "green", label: "Active" },
  invited: {
    tone: "amber",
    label: "Invited",
    note: "They haven't activated their account yet, so new records can't be added for them.",
  },
  deactivated: {
    tone: "neutral",
    label: "Deactivated",
    note: "Their records are kept for reference; new records can't be added for them.",
  },
};

interface WorkspaceBannerProps {
  /** Whose CRM this is; undefined while it loads (never another user's name meanwhile). */
  subject?: { id: string; full_name: string; status: SubjectStatus };
  /** Who is signed in: the actor every change is recorded against. */
  actor?: { id: string; fullName: string } | null;
  back: { href: string; label: string };
}

/**
 * Persistent reminder that an admin is viewing someone else's CRM. Rendered by the
 * /admin/users/[userId] layout, so it stays on every page of that workspace (lists, details,
 * forms). It separates the two people involved: the user whose records these are, and the
 * signed-in administrator, who stays the actor of every change.
 */
export function WorkspaceBanner({ subject, actor, back }: WorkspaceBannerProps) {
  const status = subject ? STATUS[subject.status] : undefined;
  const own = subject !== undefined && actor?.id === subject.id;
  return (
    <section
      aria-label="Workspace context"
      className="relative mb-6 flex flex-wrap items-start justify-between gap-x-4 gap-y-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3"
    >
      <div className="min-w-0 flex-1 text-sm text-amber-900">
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <Eye aria-hidden="true" className="size-4 shrink-0" />
          <span>Viewing CRM for:</span>{" "}
          {subject && status ? (
            <>
              <strong className="min-w-0 wrap-break-word font-semibold">
                {subject.full_name}
                {own ? " (you)" : ""}
              </strong>{" "}
              <Badge tone={status.tone}>
                <span className="sr-only">Status: </span>
                {status.label}
              </Badge>
            </>
          ) : (
            <>
              <Skeleton className="h-3.5 w-32 bg-amber-200 align-middle" />
              <span className="sr-only">Loading user name</span>
            </>
          )}
        </p>
        {actor && !own ? (
          <p className="mt-1 text-xs text-amber-800">
            Signed in as {actor.fullName}
            <span className="hidden sm:inline">. Changes you make here are recorded as yours</span>.
          </p>
        ) : null}
        {status?.note ? <p className="mt-1 text-xs text-amber-800">{status.note}</p> : null}
      </div>
      <Link
        href={back.href}
        className="inline-flex shrink-0 items-center gap-1 rounded-sm text-sm font-medium text-amber-900 hover:underline"
      >
        <ArrowLeft aria-hidden="true" className="size-4" />
        {back.label}
      </Link>
    </section>
  );
}
