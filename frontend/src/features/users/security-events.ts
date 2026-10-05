import type { SecurityEvent } from "@/lib/api/types";

import { ROLE_OPTIONS } from "./api";

const SESSION_ENDS: Record<string, string> = {
  expired: "expired",
  signed_out: "signed out",
  not_allowed: "no longer allowed",
  session_changed: "signed in again",
};

function roleLabel(role: string | undefined): string | null {
  return ROLE_OPTIONS.find((option) => option.value === role)?.label ?? null;
}

/**
 * One plain sentence per security event ("Anita Rao set a new password for Rahul Sharma").
 * Passwords never appear: the API has none to give, only that something happened.
 */
export function describeSecurityEvent(event: SecurityEvent): string {
  const actor = event.actor?.full_name ?? null;
  const user = event.user?.full_name ?? "a user";
  const by = actor ?? "Arkray";
  const self = event.actor !== null && event.user !== null && event.actor.id === event.user.id;
  switch (event.action) {
    case "auth.login_with_temporary_password":
      return `${user} signed in with a temporary password`;
    case "auth.password_changed":
      return self || !actor ? `${user} changed their password` : `${actor} changed ${user}'s password`;
    case "auth.password_reset_completed":
      return `${user} reset their password`;
    case "auth.password_set_by_admin":
      return `${by} set a new password for ${user}`;
    case "user.created":
      return `${by} created ${user}`;
    case "user.deactivated":
      return `${by} deactivated ${user}`;
    case "user.reactivated":
      return `${by} reactivated ${user}`;
    case "user.role_changed": {
      const to = roleLabel(event.details.to);
      return to ? `${by} changed ${user}'s role to ${to}` : `${by} changed ${user}'s role`;
    }
    case "user.email_changed":
      return `${by} changed ${user}'s email`;
    case "support_session.started":
      return `${by} started a support session for ${user}`;
    case "support_session.ended": {
      const end = SESSION_ENDS[event.details.end ?? ""];
      const who = actor ? `${actor}'s support session for ${user}` : `Support session for ${user}`;
      return end ? `${who} ended (${end})` : `${who} ended`;
    }
    default:
      return `Account activity for ${user}`;
  }
}
