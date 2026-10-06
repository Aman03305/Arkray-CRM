"use client";

import { useLinkStatus } from "next/link";

/**
 * Goes inside a <Link> (which must be `relative`): a thin bar confirming the click while the
 * next page is on its way, e.g. a route `next dev` is still compiling or a slow network. It
 * appears only after 120 ms (globals.css), so fast navigations show nothing. Decorative: the
 * page's own loading state is what assistive technology hears about.
 */
export function LinkPending({ className }: { className: string }) {
  const { pending } = useLinkStatus();
  return <span aria-hidden="true" className={`link-pending pointer-events-none absolute ${pending ? "is-pending" : ""} ${className}`} />;
}
