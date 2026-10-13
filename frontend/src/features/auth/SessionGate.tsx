"use client";

import { useQueryClient } from "@tanstack/react-query";
import { type ReactNode, useEffect, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { describeError, isApiError } from "@/lib/api/errors";
import { currentLocation, hardNavigate } from "@/lib/browser";
import { VIEWER_QUERY_KEY } from "@/lib/query-client";
import { ViewerProvider } from "@/lib/viewer-context";

import { useViewerQuery } from "./api";
import { ForcedPasswordChange } from "./ForcedPasswordChange";
import { MfaEnrollment } from "./MfaEnrollment";

/**
 * Loads the signed-in user for every authenticated page. Without a session the global 401
 * handler (lib/query-client) reloads onto /login; until then the shell shows skeletons,
 * never guessed data. This is UX only: the API refuses unauthenticated requests anyway.
 *
 * Someone whose password was set by an administrator must choose their own before anything
 * else: until then the password form replaces the app. Then, when the API says their account
 * must have two-step verification and it isn't set up (`mfa_enrollment_required`: ADR-0033's
 * administrators, or every account when the organisation requires it), the set-up replaces the
 * app.
 */
export function SessionGate({ children }: { children: ReactNode }) {
  const viewer = useViewerQuery();
  const queryClient = useQueryClient();
  const id = viewer.data?.id;
  // Once shown, the set-up stays until the user has saved their recovery codes: the viewer
  // stops saying "required" as soon as the first code is confirmed (a refetch on focus, say
  // after switching to a password manager), and the codes must not vanish then.
  const [enrolling, setEnrolling] = useState(false);
  if (viewer.data?.mfaEnrollmentRequired && !viewer.data.passwordChangeRequired && !enrolling) setEnrolling(true);
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
  if (enrolling) {
    const finish = async () => {
      // The app opens with the viewer as it is now (set up), never the stale "required".
      await queryClient.invalidateQueries({ queryKey: VIEWER_QUERY_KEY });
      setEnrolling(false);
    };
    return <MfaEnrollment onDone={() => void finish()} />;
  }
  return <ViewerProvider viewer={viewer.data ?? null}>{children}</ViewerProvider>;
}
