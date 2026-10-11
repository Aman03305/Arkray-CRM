"use client";

import { RotateCcw } from "lucide-react";

import { Button } from "./Button";

/** Next.js's digest of a server-side error: a short hash. Anything else is not shown. */
const DIGEST = /^[\w-]{1,64}$/;

/**
 * What an error boundary shows when a page fails unexpectedly (ARCH-10): never the error's
 * message or stack (they may hold record data or internals), only the digest Next.js gives a
 * server-side error, as a reference that matches the server logs. "Try again" re-renders the
 * failed part; the way home is a full page load, which also starts the app's state afresh.
 * Tailwind classes only: the Content-Security-Policy refuses inline style attributes.
 */
export function ErrorView({
  digest,
  onRetry,
  fullPage = false,
}: {
  digest?: string | undefined;
  onRetry: () => void;
  fullPage?: boolean;
}) {
  const reference = typeof digest === "string" && DIGEST.test(digest) ? digest : null;
  return (
    <div className={`flex flex-col items-center justify-center gap-3 px-4 text-center ${fullPage ? "min-h-dvh" : "py-24"}`}>
      {/* Announced when it replaces a page that was already on screen. */}
      <div role="alert" className="flex flex-col items-center gap-3">
        <p className="text-sm font-medium text-red-700">Error</p>
        <h1 className="text-xl font-semibold">Something went wrong</h1>
        <p className="max-w-md text-sm text-slate-500">
          This page couldn&apos;t be shown. Try again, or go back to the Dashboard. If it keeps
          happening, tell your administrator{reference ? " and quote the reference below" : ""}.
        </p>
        {reference ? <p className="text-xs text-slate-500">Reference: {reference}</p> : null}
      </div>
      <div className="mt-2 flex flex-wrap items-center justify-center gap-3">
        <Button onClick={onRetry} icon={<RotateCcw aria-hidden="true" className="size-4" />}>
          Try again
        </Button>
        <a href="/dashboard" className="text-sm font-medium text-brand-700 hover:underline">
          Go to Dashboard
        </a>
      </div>
    </div>
  );
}
