import { Ban, CalendarClock, CircleCheck, CircleDot, ListTodo, StickyNote, Users } from "lucide-react";
import Link from "next/link";

import { Badge } from "@/components/ui/Badge";
import type {
  ActivityLeadRef,
  ActivityOpportunityRef,
  ActivityPriority,
  ActivityStatus,
  ActivityType,
} from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";
import { leadHref, opportunityHref, type Workspace } from "@/lib/workspace";

const TYPES: Record<ActivityType, { label: string; icon: typeof ListTodo }> = {
  task: { label: "Task", icon: ListTodo },
  meeting: { label: "Meeting", icon: Users },
  note: { label: "Note", icon: StickyNote },
};

/** Task / Meeting / Note, always written out; the icon only reinforces the word. */
export function TypeLabel({ type, className = "" }: { type: ActivityType; className?: string }) {
  const known = TYPES[type] ?? { label: type, icon: StickyNote };
  const Icon = known.icon;
  return (
    <span className={`inline-flex items-center gap-1 text-slate-700 ${className}`}>
      <Icon aria-hidden="true" className="size-3.5 shrink-0 text-slate-500" />
      {known.label}
    </span>
  );
}

/** The type's icon alone, where the text around it already names the type. */
export function TypeIcon({ type }: { type: ActivityType }) {
  const Icon = (TYPES[type] ?? { icon: StickyNote }).icon;
  return <Icon aria-hidden="true" className="mr-1.5 inline size-3.5 shrink-0 align-[-2px] text-slate-500" />;
}

export function typeLabel(type: ActivityType): string {
  return TYPES[type]?.label ?? type;
}

const NAME_PREVIEW = 40;

/** How controls name an activity for assistive technology: a task's or meeting's subject;
 * a note (which has none) by the start of its text, so "Actions for note: Prefers morning
 * calls" and "Actions for note: Budget approved" can be told apart. */
export function activityName(activity: { type: ActivityType; title: string; preview?: string }): string {
  if (activity.type !== "note") return activity.title;
  const text = (activity.preview ?? "").replace(/\s+/g, " ").trim();
  const characters = Array.from(text);
  return characters.length ? `note: ${characters.slice(0, NAME_PREVIEW).join("")}${characters.length > NAME_PREVIEW ? "…" : ""}` : "note";
}

const STATUSES: Record<ActivityStatus, { label: string; tone: "blue" | "green" | "neutral"; icon: typeof CircleDot }> = {
  open: { label: "Open", tone: "blue", icon: CircleDot },
  scheduled: { label: "Scheduled", tone: "blue", icon: CalendarClock },
  completed: { label: "Completed", tone: "green", icon: CircleCheck },
  cancelled: { label: "Cancelled", tone: "neutral", icon: Ban },
};

/** The status in words (notes have none); colour and icon only reinforce it. */
export function StatusBadge({ status, overdue = false, type }: { status: ActivityStatus | null; overdue?: boolean; type?: ActivityType }) {
  if (!status) return <span className="text-slate-500">—</span>;
  const known = STATUSES[status] ?? STATUSES.open;
  const Icon = known.icon;
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      <Badge tone={known.tone}>
        <Icon aria-hidden="true" className="mr-1 size-3" />
        {known.label}
      </Badge>
      {overdue ? <Badge tone="red">{type === "meeting" ? "Awaiting outcome" : "Overdue"}</Badge> : null}
    </span>
  );
}

export function statusLabel(status: ActivityStatus): string {
  return STATUSES[status]?.label ?? status;
}

const PRIORITIES: Record<ActivityPriority, string> = { low: "Low", normal: "Normal", high: "High" };

export function PriorityLabel({ priority }: { priority: ActivityPriority | null }) {
  if (!priority) return <span className="text-slate-500">—</span>;
  return <span className={priority === "high" ? "font-medium text-red-700" : undefined}>{PRIORITIES[priority] ?? priority}</span>;
}

/** When it is due / starts: the date and time in the business time zone, or a dash. */
export function When({ iso, empty = "—" }: { iso: string | null; empty?: string }) {
  if (!iso) return <span className="text-slate-500">{empty}</span>;
  return <time dateTime={iso}>{formatDateTime(iso)}</time>;
}

export function scheduleOf(activity: { type: ActivityType; due_at: string | null; starts_at: string | null; created_at: string }): string | null {
  if (activity.type === "task") return activity.due_at;
  if (activity.type === "meeting") return activity.starts_at;
  return activity.created_at;
}

/** The lead an activity is about: a link, or "in another workspace" (its name isn't sent). */
export function LeadLink({ workspace, lead }: { workspace: Workspace; lead: ActivityLeadRef }) {
  if (lead.restricted || !lead.id) return <span className="italic text-slate-500">Lead in another workspace</span>;
  return (
    <Link href={leadHref(workspace, lead.id)} className="text-brand-700 hover:underline">
      {lead.display_name}
    </Link>
  );
}

export function OpportunityLink({ workspace, opportunity }: { workspace: Workspace; opportunity: ActivityOpportunityRef | null }) {
  if (!opportunity) return <span className="text-slate-500">—</span>;
  if (opportunity.restricted || !opportunity.id) {
    return <span className="italic text-slate-500">Opportunity in another workspace</span>;
  }
  return (
    <Link href={opportunityHref(workspace, opportunity.id)} className="text-brand-700 hover:underline">
      {opportunity.title}
    </Link>
  );
}
