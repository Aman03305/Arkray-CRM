import type { ReactNode } from "react";

import { BrandMark } from "@/components/shell/BrandMark";

/** The frame shared by sign-in, password reset and account activation pages. */
export function AuthCard({ title, subtitle, children, footer }: {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center bg-[linear-gradient(180deg,var(--color-shell)_0,var(--color-shell)_45%,var(--color-board)_45%)] px-4 py-12">
      <div className="mb-8 flex items-center gap-2 text-white">
        <BrandMark className="size-8 text-sm" />
        <span className="text-base font-semibold tracking-tight">Arkray CRM</span>
      </div>
      <div className="w-full max-w-sm rounded-lg border border-card-border bg-white p-6 shadow-lg sm:p-8">
        <h1 className="text-lg font-semibold tracking-tight text-slate-900">{title}</h1>
        {subtitle ? <p className="mt-1 text-sm text-slate-500">{subtitle}</p> : null}
        <div className="mt-6">{children}</div>
      </div>
      {footer ? <div className="mt-6 text-sm text-slate-500">{footer}</div> : null}
    </main>
  );
}

export const PASSWORD_POLICY_HINT = "At least 12 characters. Not a common password, and not based on a name or email.";
