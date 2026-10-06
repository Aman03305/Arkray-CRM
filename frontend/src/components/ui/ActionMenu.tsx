"use client";

import { MoreHorizontal } from "lucide-react";
import { type KeyboardEvent, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface MenuAction {
  key: string;
  label: string;
  onSelect: () => void;
  tone?: "default" | "danger";
}

const GAP = 4;
const MARGIN = 8;

/**
 * A "more actions" menu button (WAI-ARIA menu button pattern): Enter/Space/ArrowDown open
 * it, arrow keys move between items, Escape closes and returns focus to the button.
 * The menu is portalled and positioned against the viewport, so a scrolling table can't
 * clip it; near the bottom of the screen it opens upwards. While `disabled` (an action on
 * its row is running) it stays focusable but doesn't open (aria-disabled, so focus coming
 * back to it from the menu isn't lost).
 */
export function ActionMenu({ label, actions, disabled = false }: { label: string; actions: readonly MenuAction[]; disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ top: number; right: number } | null>(null);
  const menuId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLUListElement>(null);

  const items = () => Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? []);
  const close = (restoreFocus: boolean) => {
    setOpen(false);
    setPosition(null);
    if (restoreFocus) trigger.current?.focus();
  };

  useLayoutEffect(() => {
    if (!open || !trigger.current || !menu.current) return;
    const anchor = trigger.current.getBoundingClientRect();
    const height = menu.current.offsetHeight;
    const below = anchor.bottom + GAP;
    const fitsBelow = below + height <= window.innerHeight - MARGIN;
    setPosition({
      top: fitsBelow ? below : Math.max(MARGIN, anchor.top - GAP - height),
      right: Math.max(MARGIN, window.innerWidth - anchor.right),
    });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    items()[0]?.focus();
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!menu.current?.contains(target) && !trigger.current?.contains(target)) close(false);
    };
    const onViewportChange = () => close(false); // a fixed menu would drift from its row
    document.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("resize", onViewportChange);
    window.addEventListener("scroll", onViewportChange, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("resize", onViewportChange);
      window.removeEventListener("scroll", onViewportChange, true);
    };
  }, [open]);

  const onMenuKeyDown = (event: KeyboardEvent) => {
    const list = items();
    const index = list.indexOf(document.activeElement as HTMLButtonElement);
    const focusAt = (i: number) => list[(i + list.length) % list.length]?.focus();
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        focusAt(index + 1);
        break;
      case "ArrowUp":
        event.preventDefault();
        focusAt(index - 1);
        break;
      case "Home":
        event.preventDefault();
        focusAt(0);
        break;
      case "End":
        event.preventDefault();
        focusAt(list.length - 1);
        break;
      case "Escape":
        event.preventDefault();
        close(true);
        break;
      case "Tab":
        event.preventDefault();
        close(true);
        break;
    }
  };

  if (actions.length === 0) return null;
  return (
    <div className="inline-block text-left">
      <button
        ref={trigger}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open && !disabled}
        aria-controls={open && !disabled ? menuId : undefined}
        aria-disabled={disabled || undefined}
        onClick={() => {
          if (disabled) return;
          if (open) close(false);
          else setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            if (!disabled) setOpen(true);
          }
        }}
        // 32 px to hit, taking the 28 px of room it always had (rows and cards keep their height).
        className="-m-0.5 rounded-md p-2 text-slate-500 hover:bg-slate-100 hover:text-slate-800 aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
      >
        <MoreHorizontal aria-hidden="true" className="size-4" />
      </button>
      {open && !disabled
        ? createPortal(
            <ul
              ref={menu}
              id={menuId}
              role="menu"
              aria-label={label}
              onKeyDown={onMenuKeyDown}
              style={position ? { top: position.top, right: position.right } : { top: -9999, right: 0 }}
              className="fixed z-40 w-52 rounded-md border border-slate-200 bg-white py-1 shadow-lg"
            >
              {actions.map((action) => (
                <li key={action.key} role="none">
                  <button
                    type="button"
                    role="menuitem"
                    tabIndex={-1}
                    onClick={() => {
                      close(true);
                      action.onSelect();
                    }}
                    className={`block w-full px-3 py-2 text-left text-sm hover:bg-slate-50 focus:bg-slate-100 focus:outline-none ${
                      action.tone === "danger" ? "text-red-700" : "text-slate-700"
                    }`}
                  >
                    {action.label}
                  </button>
                </li>
              ))}
            </ul>,
            document.body,
          )
        : null}
    </div>
  );
}
