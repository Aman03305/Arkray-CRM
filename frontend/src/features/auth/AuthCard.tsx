import type { ReactNode } from "react";

/** The frame shared by sign-in, password reset and account activation pages. */
export function AuthCard({ title, subtitle, children, footer }: {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <main className="flex min-h-dvh flex-col items-center justify-center px-4 py-12">
      <div className="mb-8 flex items-center gap-2">
        <span aria-hidden="true" className="flex size-8 items-center justify-center rounded-md bg-brand-600 text-sm font-bold text-white">
          A
        </span>
        <span className="text-base font-semibold tracking-tight">Arkray CRM</span>
      </div>
      <div className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 shadow-sm sm:p-8">
        <h1 className="text-lg font-semibold tracking-tight text-slate-900">{title}</h1>
        {subtitle ? <p className="mt-1 text-sm text-slate-500">{subtitle}</p> : null}
        <div className="mt-6">{children}</div>
      </div>
      {footer ? <div className="mt-6 text-sm text-slate-500">{footer}</div> : null}
    </main>
  );
}

export const PASSWORD_POLICY_HINT =
  "At least 12 characters. A few unrelated words make a strong, memorable password. Common passwords and ones based on your name or email are not accepted.";
