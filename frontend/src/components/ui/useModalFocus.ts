"use client";

import { type RefObject, useEffect, useRef } from "react";

export const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Elements Tab can reach: focusable and not taken out of the tab order (`tabindex="-1"`,
 * e.g. combobox options, which focus never moves to). */
function tabbable(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((el) => el.tabIndex >= 0);
}

/** Make everything outside `element` inert (not focusable, hidden from assistive tech). */
function inertOutside(element: HTMLElement): () => void {
  const changed: HTMLElement[] = [];
  let node: HTMLElement | null = element;
  while (node && node !== document.body && node.parentElement) {
    for (const sibling of Array.from(node.parentElement.children)) {
      if (sibling !== node && sibling instanceof HTMLElement && !sibling.inert) {
        sibling.inert = true;
        changed.push(sibling);
      }
    }
    node = node.parentElement;
  }
  return () => changed.forEach((el) => (el.inert = false));
}

/** Open modals, innermost last: only the top one answers keys and focus (a confirmation
 * opened from a drawer closes alone on Escape; enhancement review). */
const openModals: symbol[] = [];
const isTop = (modal: symbol) => openModals[openModals.length - 1] === modal;

/**
 * Modal focus management (dialogs, the mobile navigation drawer): on open, focus moves in
 * (to `[data-autofocus]`, else the first focusable element, else the container); Tab and
 * Shift+Tab cycle inside, and focus that escapes (for example a control disabling itself)
 * is pulled back; the rest of the page is inert; Escape calls `onEscape`; on close, focus
 * returns to whatever had it before.
 */
export function useModalFocus(
  open: boolean,
  container: RefObject<HTMLElement | null>,
  onEscape: () => void,
  /** The whole overlay (panel plus backdrop); everything outside it becomes inert. */
  boundary: RefObject<HTMLElement | null> = container,
): void {
  const onEscapeRef = useRef(onEscape);
  useEffect(() => {
    onEscapeRef.current = onEscape;
  });

  useEffect(() => {
    const root = container.current;
    if (!open || !root) return;
    const overlay = boundary.current ?? root;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // A disabled element can't take focus: skip it rather than leave focus behind the modal.
    const first = root.querySelector<HTMLElement>("[data-autofocus]:not(:disabled)") ?? tabbable(root)[0];
    (first ?? root).focus();
    const restoreInert = inertOutside(overlay);
    const modal = Symbol("modal");
    openModals.push(modal);

    const onKeyDown = (event: KeyboardEvent) => {
      if (!isTop(modal)) return; // a modal opened on top of this one handles it
      if (event.key === "Escape") {
        if (event.isComposing) return; // the input method cancels its own composition
        event.stopPropagation();
        onEscapeRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = tabbable(root);
      if (focusable.length === 0) {
        event.preventDefault();
        root.focus();
        return;
      }
      const firstEl = focusable[0]!;
      const lastEl = focusable[focusable.length - 1]!;
      const active = document.activeElement;
      if (!root.contains(active)) {
        event.preventDefault();
        (event.shiftKey ? lastEl : firstEl).focus();
      } else if (event.shiftKey && active === firstEl) {
        event.preventDefault();
        lastEl.focus();
      } else if (!event.shiftKey && active === lastEl) {
        event.preventDefault();
        firstEl.focus();
      }
    };
    const onFocusIn = (event: FocusEvent) => {
      if (!isTop(modal)) return;
      if (event.target instanceof Node && !root.contains(event.target)) root.focus();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", onFocusIn);
    return () => {
      openModals.splice(openModals.indexOf(modal), 1);
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", onFocusIn);
      restoreInert();
      if (opener?.isConnected) opener.focus();
      else focusPageHeading();
    };
  }, [open, container, boundary]);
}

/**
 * Where focus goes when the control that opened a dialog is gone once it closes (for
 * example "Mark as won" is replaced by "Reopen", or "Archive" by "Restore"): the page's
 * main heading, so keyboard and screen-reader users stay on the page instead of being
 * thrown back to the start of the document (Phase 3 review).
 */
function focusPageHeading(): void {
  const heading = document.querySelector<HTMLElement>("main h1") ?? document.querySelector<HTMLElement>("main");
  if (!heading) return;
  if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
  heading.focus();
}
