"use client";

import { Alert } from "@/components/ui/Alert";
import { describeError } from "@/lib/api/errors";

import { AuthCard } from "./AuthCard";
import { MfaSetupFlow } from "./MfaSetupFlow";
import { useSignOut } from "./useSignOut";

/**
 * Shown instead of the app while two-step verification is required for the viewer but not set
 * up (ADR-0033: administrators; every account when the organisation requires it, review of
 * 73ff1e3 L1). The API says so (`mfa_enrollment_required`), not the role, and refuses
 * everything else meanwhile (403 `mfa_enrollment_required`). `onDone` runs once the recovery
 * codes are saved; the session gate then fetches the viewer again and the app opens.
 */
export function MfaEnrollment({ onDone }: { onDone: () => void }) {
  const signOut = useSignOut();
  const signOutError = signOut.isError ? describeError(signOut.error) : null;
  return (
    <AuthCard
      title="Set up two-step verification"
      subtitle="Your account needs a second step at sign-in: a code from an app on your phone. It takes a minute."
      footer={
        <button
          type="button"
          onClick={() => signOut.mutate()}
          disabled={signOut.isPending || signOut.isSuccess}
          className="rounded-sm font-medium text-brand-700 hover:underline disabled:opacity-50"
        >
          Sign out
        </button>
      }
    >
      {signOutError ? (
        <div className="mb-4">
          <Alert tone="error" requestId={signOutError.requestId}>
            {signOutError.message}
          </Alert>
        </div>
      ) : null}
      <MfaSetupFlow onComplete={onDone} />
    </AuthCard>
  );
}
