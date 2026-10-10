import { CircleDot, CircleX, Trophy } from "lucide-react";

import { Badge } from "@/components/ui/Badge";
import type { OpportunityLeadRef, Stage, StageCategory } from "@/lib/api/types";
import { businessToday, formatDateOnly } from "@/lib/format";
import { formatInr } from "@/lib/money";

const OUTCOMES: Record<StageCategory, { label: string; tone: "blue" | "green" | "neutral"; icon: typeof Trophy }> = {
  open: { label: "Open", tone: "blue", icon: CircleDot },
  won: { label: "Won", tone: "green", icon: Trophy },
  lost: { label: "Lost", tone: "neutral", icon: CircleX },
};

/** Open / Won / Lost, always written out; colour and icon only reinforce the word. */
export function OutcomeBadge({ status }: { status: StageCategory }) {
  const outcome = OUTCOMES[status] ?? OUTCOMES.open;
  const Icon = outcome.icon;
  return (
    <Badge tone={outcome.tone}>
      <Icon aria-hidden="true" className="mr-1 size-3" />
      {outcome.label}
    </Badge>
  );
}

/** A stage's name, with its outcome for won and lost stages ("Won", "Lost"). */
export function StageName({ stage }: { stage: Pick<Stage, "name" | "category" | "is_active"> }) {
  const Icon = stage.category === "won" ? Trophy : stage.category === "lost" ? CircleX : null;
  return (
    <span className="inline-flex items-center gap-1">
      {Icon ? <Icon aria-hidden="true" className="size-3.5 shrink-0" /> : null}
      {stage.name}
      {stage.is_active ? null : <span className="text-xs font-normal text-slate-500">(retired)</span>}
    </span>
  );
}

/** An amount in rupees; the exact figure (with paise) is in the tooltip. */
export function Amount({ value, className = "" }: { value: string; className?: string }) {
  return (
    <span className={`tabular-nums ${className}`} title={formatInr(value, { paise: "always" })}>
      {formatInr(value)}
    </span>
  );
}

/**
 * The customer an opportunity is for (its customer record, the API's `lead`). "Restricted"
 * when the record has moved to someone else's workspace (a won or lost opportunity keeps
 * the owner who closed it): its name is not sent, so it can't be shown.
 */
export function CustomerName({ lead }: { lead: OpportunityLeadRef }) {
  if (lead.restricted || !lead.id) {
    return <span className="italic text-slate-500">Customer in another workspace</span>;
  }
  return <>{lead.display_name}</>;
}

/** Why a deal shows no customer details (the API's `customer_restricted`). */
export const RESTRICTED_CUSTOMER_NOTE =
  "Customer details hidden: this customer was reassigned to someone else after the deal closed.";

/**
 * In place of a deal's customer details when the viewer may no longer see them: a closed deal
 * whose customer moved to someone else keeps its commercial history here, but not the
 * customer's name, contacts, address or free-text details (docs/authorization.md#historical-deals).
 * Said once, plainly, rather than as a row of empty fields.
 */
export function RestrictedCustomerNote({ className = "" }: { className?: string }) {
  return <p className={`rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-600 ${className}`}>{RESTRICTED_CUSTOMER_NOTE}</p>;
}

/** An expected close date; "Overdue" (in words) when an open opportunity's date has passed. */
export function CloseDate({ date, open, today = businessToday() }: { date: string | null; open: boolean; today?: string }) {
  if (!date) return <span className="text-slate-500">No close date</span>;
  const overdue = open && date < today;
  return (
    <span className={overdue ? "text-red-700" : undefined}>
      <time dateTime={date}>{formatDateOnly(date)}</time>
      {overdue ? <span className="ml-1 text-xs font-medium">(overdue)</span> : null}
    </span>
  );
}
