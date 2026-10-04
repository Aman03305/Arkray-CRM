"use client";

import { X } from "lucide-react";
import { type ReactNode, useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

import { useModalFocus } from "./useModalFocus";

const SIZES = { sm: "max-w-md", md: "max-w-lg", lg: "max-w-2xl" } as const;

interface DialogProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  /** Called on Escape, the close button or a backdrop click, unless `busy`. */
  onClose: () => void;
  /** While an action runs the dialog cannot be dismissed (no half-finished actions). */
  busy?: boolean;
  children: ReactNode;
  size?: "sm" | "md" | "lg";
  /** "alertdialog" for confirmations of consequential actions. */
  role?: "dialog" | "alertdialog";
}

/**
 * Modal dialog: labelled and described, focus moves in on open and stays inside (even if
 * the focused control disables itself), the page behind is inert, Escape closes, and focus
 * returns to the element that opened it.
 */
export function Dialog({
  open,
  title,
  description,
  onClose,
  busy = false,
  children,
  size = "md",
  role = "dialog",
}: DialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const busyRef = useRef(busy);

  useEffect(() => {
    busyRef.current = busy;
    // Keep focus inside while an action runs (the focused button may have gone inert).
    if (busy && panel.current && !panel.current.contains(document.activeElement)) panel.current.focus();
  }, [busy]);

  useModalFocus(
    open,
    panel,
    () => {
      if (!busyRef.current) onClose();
    },
    container,
  );

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  if (!open) return null;
  return createPortal(
    <div ref={container} className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:items-center">
      <div
        aria-hidden="true"
        className="fixed inset-0 bg-slate-900/40"
        onClick={() => {
          if (!busy) onClose();
        }}
      />
      <div
        ref={panel}
        role={role}
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        aria-busy={busy || undefined}
        tabIndex={-1}
        className={`relative w-full rounded-lg bg-white shadow-xl focus:outline-none ${SIZES[size]}`}
      >
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-4">
          <div className="min-w-0 [overflow-wrap:anywhere]">
            <h2 id={titleId} className="text-base font-semibold text-slate-900">
              {title}
            </h2>
            {description ? (
              <div id={descriptionId} className="mt-1 space-y-2 text-sm text-slate-500">
                {description}
              </div>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label="Close dialog"
            className="-mr-1 rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-50"
          >
            <X aria-hidden="true" className="size-4" />
          </button>
        </div>
        <div className="px-5 py-4">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

export function DialogActions({ children }: { children: ReactNode }) {
  return <div className="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">{children}</div>;
}
