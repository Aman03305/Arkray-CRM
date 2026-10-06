"use client";

import { type ReactNode, useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { describeError, isApiError } from "@/lib/api/errors";
import { currentLocation, hardNavigate } from "@/lib/browser";
import { ViewerProvider } from "@/lib/viewer-context";

import { useViewerQuery } from "./api";
import { ForcedPasswordChange } from "./ForcedPasswordChange";

/**
 * Loads the signed-in user for every authenticated page. Without a session the global 401
 * handler (lib/query-client) reloads onto /login; until then the shell shows skeletons,
 * never guessed data. This is UX only: the API refuses unauthenticated requests anyway.
 *
 * Someone whose password was set by an administrator must choose their own before anything
 * else: until then the password form replaces the app.
 */
export function SessionGate({ children }: { children: ReactNode }) {
  const viewer = useViewerQuery();
  const id = viewer.data?.id;
  // Who this page was loaded for: the first viewer it saw.
  const [signedInAs, setSignedInAs] = useState<string | null>(null);
  if (id && signedInAs === null) setSignedInAs(id);
  // Someone else signed in on this browser meanwhile (another tab; /auth/me is asked again
  // whenever the tab regains focus): reload rather than mix identities. Until the page goes,
  // nothing renders: the cache still holds the previous person's records, and the shell would
  // show them under the new person's name.
  const switched = Boolean(id && signedInAs && signedInAs !== id);
  useEffect(() => {
    if (switched) hardNavigate(currentLocation());
  }, [switched]);
  if (switched) return null;

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

  if (viewer.data?.passwordChangeRequired) return <ForcedPasswordChange viewer={viewer.data} />;
  return <ViewerProvider viewer={viewer.data ?? null}>{children}</ViewerProvider>;
}
