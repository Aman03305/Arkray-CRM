"use client";

import { ErrorView } from "@/components/ui/ErrorView";

/**
 * A page inside the signed-in shell failed unexpectedly (ARCH-10): the shell stays, with
 * navigation, and its <main> holds this view (no second main landmark, as not-found).
 * `retry` re-fetches and re-renders the page. The error itself is never shown.
 */
export default function AppError({ error, retry }: { error: Error & { digest?: string }; retry: () => void }) {
  return <ErrorView digest={error.digest} onRetry={retry} />;
}
