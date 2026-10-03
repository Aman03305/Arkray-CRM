import { Badge } from "@/components/ui/Badge";
import type { AdminUser } from "@/lib/api/types";
import { formatDateTime, formatRelative } from "@/lib/format";

const BADGE = {
  active: { tone: "green", label: "Active" },
  invited: { tone: "amber", label: "Invited" },
  deactivated: { tone: "neutral", label: "Deactivated" },
} as const;

/** Status badge, plus the invitation's delivery and expiry for invited users. */
export function UserStatus({ user, now }: { user: AdminUser; now?: Date }) {
  const badge = BADGE[user.status];
  const invitation = user.invitation;
  let detail: { text: string; title?: string; urgent?: boolean } | null = null;
  if (user.status === "invited" && !invitation) {
    // e.g. revoked because the admin who sent it was removed: resend to issue a new link.
    detail = { text: "No active invitation", urgent: true };
  } else if (invitation) {
    if (invitation.expired) {
      detail = { text: "Invitation expired", urgent: true };
    } else if (!invitation.sent_at) {
      detail = { text: "Sending invitation…" };
    } else {
      detail = {
        text: `Link expires ${formatRelative(invitation.expires_at, now)}`,
        title: formatDateTime(invitation.expires_at),
      };
    }
  }
  return (
    <div className="flex flex-col items-start gap-1">
      <Badge tone={badge.tone}>{badge.label}</Badge>
      {detail ? (
        <span title={detail.title} className={`text-xs ${detail.urgent ? "text-red-600" : "text-slate-500"}`}>
          {detail.text}
        </span>
      ) : null}
    </div>
  );
}
