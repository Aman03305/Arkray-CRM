import { Flame, Snowflake, Sun } from "lucide-react";

import { Badge } from "@/components/ui/Badge";
import type { LeadStatusRef, Rating, UserRef } from "@/lib/api/types";

const CATEGORY_TONES = {
  open: "blue",
  qualified: "green",
  unqualified: "neutral",
  converted: "amber",
} as const;

/** The status name is always written out: colour only reinforces it. */
export function StatusBadge({ status }: { status: LeadStatusRef }) {
  return <Badge tone={CATEGORY_TONES[status.category] ?? "neutral"}>{status.name}</Badge>;
}

const RATINGS: Record<Rating, { label: string; icon: typeof Flame; className: string }> = {
  hot: { label: "Hot", icon: Flame, className: "text-red-700" },
  warm: { label: "Warm", icon: Sun, className: "text-amber-700" },
  cold: { label: "Cold", icon: Snowflake, className: "text-sky-700" },
};

export function RatingLabel({ rating }: { rating: Rating | null | undefined }) {
  if (!rating) return <span className="text-slate-500">—</span>;
  const known = RATINGS[rating] as (typeof RATINGS)[Rating] | undefined;
  if (!known) return <>{rating}</>; // a rating added on the server later: show it as text
  const { label, icon: Icon, className } = known;
  return (
    <span className={`inline-flex items-center gap-1 ${className}`}>
      <Icon aria-hidden="true" className="size-3.5" />
      {label}
    </span>
  );
}

export function PersonName({ person }: { person: UserRef }) {
  return (
    <>
      {person.full_name}
      {person.is_active ? null : <span className="ml-1 text-xs text-slate-500">(deactivated)</span>}
    </>
  );
}

const EXTENSION = /\s*(?:;ext=|extension|ext\.?|x|#)\s*([0-9]+)$/i;

/**
 * A dialable tel: URI (RFC 3966) for a number as the user typed it. The extension is kept
 * as ";ext=", never glued onto the number (which would dial a different number).
 */
export function telHref(value: string): string {
  const match = EXTENSION.exec(value);
  const number = (match ? value.slice(0, match.index) : value).replace(/[^\d+]/g, "");
  return `tel:${number}${match ? `;ext=${match[1]}` : ""}`;
}

/**
 * A mailto: link that opens a draft to this one address and nothing else. Characters that
 * delimit mailto headers are encoded (RFC 6068), so a stored address such as
 * "x?bcc=spy@evil.example&body=..." can't add recipients or text (Phase 9 review; the API
 * also refuses such addresses now, this covers ones stored before).
 */
export function mailtoHref(email: string): string {
  return `mailto:${email.replace(/[%?&=#,;\s"<>\\]/g, (char) => encodeURIComponent(char))}`;
}

export function primaryPhone(lead: { phone: string; mobile: string }): string {
  return lead.phone || lead.mobile;
}

export function Muted({ children }: { children?: string | null }) {
  return children ? <>{children}</> : <span className="text-slate-500">—</span>;
}
