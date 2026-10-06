"use client";

import { type ReactNode, useEffect, useId, useRef, useState } from "react";

interface HeaderMenuProps {
  /** The button's accessible name (also its tooltip). */
  label: string;
  /** What the button shows; decorative (aria-hidden), the label names it. */
  trigger: ReactNode;
  triggerClassName: string;
  /** The panel's content; `close` closes it (after following one of its links). */
  children: (close: () => void) => ReactNode;
}

/** Shared styles for the links and buttons inside a header menu. */
export const HEADER_MENU_ITEM =
  "flex w-full items-center gap-2.5 px-3.5 py-2 text-left text-sm text-slate-700 hover:bg-slate-50 hover:text-slate-900 focus-visible:-outline-offset-2 disabled:opacity-50";

/**
 * A header button that opens a small panel of links (the disclosure pattern): Escape closes
 * it and returns focus to the button; a click or focus outside closes it.
 */
export function HeaderMenu({ label, trigger, triggerClassName, children }: HeaderMenuProps) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const button = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!panel.current?.contains(target) && !button.current?.contains(target)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      button.current?.focus();
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div
      className="relative"
      onBlur={(event) => {
        const next = event.relatedTarget as Node | null;
        if (open && next && !event.currentTarget.contains(next)) setOpen(false);
      }}
    >
      <button
        ref={button}
        type="button"
        title={label}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((value) => !value)}
        className={triggerClassName}
      >
        {/* Named by hidden text, not aria-label: an aria-label must contain what the button
            shows (WCAG 2.5.3), and initials ("RS") would only make the name noisier. The
            trigger itself is decorative (aria-hidden). */}
        <span className="sr-only">{label}</span>
        {trigger}
      </button>
      {open ? (
        <div
          ref={panel}
          id={panelId}
          className="absolute right-0 top-full z-40 mt-2 w-64 overflow-hidden rounded-lg border border-slate-200 bg-white py-1.5 text-slate-900 shadow-lg"
        >
          {children(() => setOpen(false))}
        </div>
      ) : null}
    </div>
  );
}
