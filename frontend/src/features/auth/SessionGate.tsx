"use client";

import { type ReactNode, useEffect, useRef } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { describeError, isApiError } from "@/lib/api/errors";
import { currentLocation, hardNavigate } from "@/lib/browser";
import { ViewerProvider } from "@/lib/viewer-context";

import { useViewerQuery } from "./api";

/**
 * Loads the signed-in user for every authenticated page. Without a session the global 401
 * handler (lib/query-client) reloads onto /login; until then the shell shows skeletons,
 * never guessed data. This is UX only: the API refuses unauthenticated requests anyway.
 */
export function SessionGate({ children }: { children: ReactNode }) {
  const viewer = useViewerQuery();
  const signedInAs = useRef<string | null>(null);

  // Someone else signed in on this browser meanwhile: reload rather than mix identities.
  useEffect(() => {
    const id = viewer.data?.id;
    if (!id) return;
    if (signedInAs.current && signedInAs.current !== id) hardNavigate(currentLocation());
    signedInAs.current = id;
  }, [viewer.data?.id]);

  if (viewer.isError && !isApiError(viewer.error, 401)) {
    const { message, requestId } = describeError(viewer.error);
    return (
      <main className="flex min-h-dvh items-center justify-center px-4">
        <div className="w-full max-w-md">
          <Alert
            tone="error"
            title="We couldn't load your account"
            requestId={requestId}
            action={
              <Button variant="secondary" size="sm" onClick={() => void viewer.refetch()} loading={viewer.isFetching}>
                Try again
              </Button>
            }
          >
            {message}
          </Alert>
        </div>
      </main>
    );
  }

  return <ViewerProvider viewer={viewer.data ?? null}>{children}</ViewerProvider>;
}
