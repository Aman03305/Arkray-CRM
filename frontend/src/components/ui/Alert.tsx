import { CircleAlert, CircleCheck, Info } from "lucide-react";
import type { ReactNode } from "react";

type Tone = "error" | "success" | "info";

const TONES: Record<Tone, { box: string; icon: typeof Info }> = {
  error: { box: "border-red-200 bg-red-50 text-red-800", icon: CircleAlert },
  success: { box: "border-emerald-200 bg-emerald-50 text-emerald-800", icon: CircleCheck },
  info: { box: "border-slate-200 bg-slate-50 text-slate-700", icon: Info },
};

interface AlertProps {
  tone?: Tone;
  title?: string;
  children?: ReactNode;
  /** Shown so users can quote it to support; matches the server logs. */
  requestId?: string | null;
  action?: ReactNode;
}

/** Errors are announced immediately (role="alert"); other messages politely. */
export function Alert({ tone = "info", title, children, requestId, action }: AlertProps) {
  const { box, icon: Icon } = TONES[tone];
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`flex gap-3 rounded-md border px-3.5 py-3 text-sm ${box}`}>
      <Icon aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0 flex-1">
        {title ? <p className="font-medium">{title}</p> : null}
        {children ? <div className={title ? "mt-0.5" : undefined}>{children}</div> : null}
        {requestId ? <p className="mt-1 text-xs opacity-75">Reference: {requestId}</p> : null}
        {action ? <div className="mt-2">{action}</div> : null}
      </div>
    </div>
  );
}
