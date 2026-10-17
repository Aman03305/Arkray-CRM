import type { ReactNode } from "react";

import type { AdminUser } from "@/lib/api/types";

/** One fact: the label beside its value, or above it on a phone, where the value needs the width. */
function Fact({ label, strong = false, children }: { label: string; strong?: boolean; children: ReactNode }) {
  return (
    <div className="sm:grid sm:grid-cols-[4rem_minmax(0,1fr)] sm:gap-x-3">
      <dt className="text-xs text-slate-500 sm:text-sm">{label}</dt>
      <dd className={`text-slate-900 [overflow-wrap:anywhere]${strong ? " font-medium" : ""}`}>{children}</dd>
    </div>
  );
}

/**
 * Whose account a confirmation is about: name, email and role, so the administrator checks
 * they picked the right person before something that can't be taken back (a reset link, a
 * deletion). Read out with the dialog's description. Long names and addresses wrap, and on a
 * 320 px phone each label sits above its value, so an ordinary address stays on one line.
 */
export function AccountSummary({ user }: { user: Pick<AdminUser, "full_name" | "email" | "role_label"> }) {
  return (
    <dl className="space-y-1.5 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm sm:space-y-1">
      <Fact label="Name" strong>
        {user.full_name}
      </Fact>
      <Fact label="Email">{user.email}</Fact>
      <Fact label="Role">{user.role_label}</Fact>
    </dl>
  );
}
