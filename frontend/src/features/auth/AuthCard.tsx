import type { ReactNode } from "react";

import { ArkrayLogo } from "@/components/shell/ArkrayLogo";

/** The frame shared by sign-in, password reset and account activation pages: the Arkray
 * logo heads the card (it is grey, for a light background), above the page's title. */
export function AuthCard({ title, subtitle, children, footer }: {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center bg-[linear-gradient(180deg,var(--color-shell)_0,var(--color-shell)_45%,var(--color-board)_45%)] px-4 py-12">
      <div className="w-full max-w-sm rounded-lg border border-card-border bg-white p-6 shadow-lg sm:p-8">
        <ArkrayLogo className="mx-auto mb-6 h-14 w-auto" />
        <h1 className="text-center text-lg font-semibold tracking-tight text-slate-900">{title}</h1>
        {subtitle ? <p className="mt-1 text-center text-sm text-slate-500">{subtitle}</p> : null}
        <div className="mt-6">{children}</div>
      </div>
      {footer ? <div className="mt-6 text-sm text-slate-500">{footer}</div> : null}
    </main>
  );
}

/** The sign-in page's title (its loading fallback shows the same). */
export const LOGIN_TITLE = "Welcome to Arkray";

export const PASSWORD_POLICY_HINT = "At least 12 characters. Not a common password, and not based on a name or email.";
