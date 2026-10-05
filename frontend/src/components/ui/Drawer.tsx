"use client";

import { X } from "lucide-react";
import { type ReactNode, useEffect, useId, useRef } from "react";
import { createPortal } from "react-dom";

import { useModalFocus } from "./useModalFocus";

const WIDTHS = { md: "sm:max-w-md", lg: "sm:max-w-xl", xl: "sm:max-w-2xl" } as const;

interface DrawerProps {
  open: boolean;
  title: string;
  /** One short line under the title, if any. */
  description?: ReactNode;
  /** Escape, the close button or a click on the page behind, unless `busy`. */
  onClose: () => void;
  busy?: boolean;
  children: ReactNode;
  /** Sticky at the bottom (the form's actions), so they stay reachable on long forms. */
  footer?: ReactNode;
  width?: keyof typeof WIDTHS;
}

/**
 * A side panel sliding in from the right, for creating or editing a record without leaving
 * the page: on wide screens it has a bounded width and the page stays visible behind it; on
 * phones it takes the whole width. Modal like Dialog: labelled, focus moves in and stays
 * inside, the page behind is inert, Escape closes, focus returns to the opener.
 */
export function Drawer({
  open,
  title,
  description,
  onClose,
  busy = false,
  children,
  footer,
  width = "lg",
}: DrawerProps) {
  const titleId = useId();
  const descriptionId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const container = useRef<HTMLDivElement>(null);
  const busyRef = useRef(busy);

  useEffect(() => {
    busyRef.current = busy;
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
    <div ref={container} className="fixed inset-0 z-50 flex justify-end">
      <div
        aria-hidden="true"
        className="fixed inset-0 bg-slate-900/30"
        onClick={() => {
          if (!busy) onClose();
        }}
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        aria-busy={busy || undefined}
        tabIndex={-1}
        data-drawer=""
        className={`relative flex h-full w-full flex-col bg-white shadow-2xl focus:outline-none ${WIDTHS[width]}`}
      >
        <div className="flex items-start justify-between gap-4 border-b border-slate-200 px-5 py-3.5">
          <div className="min-w-0 [overflow-wrap:anywhere]">
            <h2 id={titleId} className="text-base font-semibold text-slate-900">
              {title}
            </h2>
            {description ? (
              <div id={descriptionId} className="mt-0.5 text-sm text-slate-500">
                {description}
              </div>
            ) : null}
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={busy}
            aria-label="Close panel"
            className="-mr-1 rounded-md p-1.5 text-slate-500 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-50"
          >
            <X aria-hidden="true" className="size-4" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer ? (
          <div className="flex flex-col-reverse gap-2 border-t border-slate-200 bg-white px-5 py-3 sm:flex-row sm:justify-end">
            {footer}
          </div>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
