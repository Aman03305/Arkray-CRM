"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { describeError, isApiError } from "@/lib/api/errors";

import { authApi } from "./api";
import { AuthCard } from "./AuthCard";

const BACK_TO_SIGN_IN = (
  <Link href="/login" prefetch={false} className="font-medium text-brand-700 hover:underline">
    Back to sign in
  </Link>
);

/**
 * The link mailed to an account's owner when too many failed sign-ins in a day locked its
 * sign-in from new browsers (review of 73ff1e3, L1). A button, not an automatic request: a
 * mail scanner that opens links must not use it up. Lifting the lock also trusts this browser
 * for the account (unless it is trusted for another account: that stays), so a new wave of
 * guesses can't lock it out again. The page names the account, masked, before the button is
 * pressed (review round 1: someone else's link pressed in your browser).
 */
export function UnlockSignIn({ token }: { token: string }) {
  const preview = useQuery({
    queryKey: ["sign-in-unlock", token],
    queryFn: () => authApi.verifySignInUnlock(token),
    retry: false,
    gcTime: 0,
  });
  const unlock = useMutation({ mutationFn: () => authApi.unlockSignIn(token) });

  if (unlock.isSuccess) {
    return (
      <AuthCard title="Sign-in unlocked" footer={BACK_TO_SIGN_IN}>
        <Alert tone="success">You can sign in from this browser again.</Alert>
        <p className="mt-4 text-sm text-slate-500">
          If the failed attempts weren&apos;t you, someone may be guessing your password: choose a
          strong one you don&apos;t use anywhere else, and tell your administrator.
        </p>
      </AuthCard>
    );
  }

  const invalid = isApiError(unlock.error, 400, "invalid_token")
    ? unlock.error
    : isApiError(preview.error, 400, "invalid_token")
      ? preview.error
      : null;
  if (invalid) {
    return (
      <AuthCard title="This link can't be used" footer={BACK_TO_SIGN_IN}>
        <Alert tone="error">{invalid.message}</Alert>
        <p className="mt-4 text-sm text-slate-500">
          Unlock links work once and expire after a day. Resetting your password lifts the lock
          too.
        </p>
        <Link
          href="/forgot-password"
          prefetch={false}
          className="mt-4 inline-flex h-9 w-full items-center justify-center rounded-full bg-brand-600 text-sm font-medium text-white hover:bg-brand-700"
        >
          Reset your password
        </Link>
      </AuthCard>
    );
  }

  const failure = unlock.error ?? preview.error;
  const banner = failure ? describeError(failure, { tooMany: "attempts" }) : null;
  return (
    <AuthCard
      title="Unlock sign-in to your account"
      subtitle="Too many failed sign-ins locked your account from browsers it hasn't been used in."
      footer={BACK_TO_SIGN_IN}
    >
      <div className="space-y-4">
        {banner ? (
          <Alert tone="error" requestId={banner.requestId}>
            {banner.message}
          </Alert>
        ) : null}
        {preview.data ? (
          <p className="text-sm text-slate-900">
            This link is for the account <strong>{preview.data.email}</strong>. If that isn&apos;t
            yours, close this page.
          </p>
        ) : null}
        <p className="text-sm text-slate-600">
          Lifting the lock also lets this browser sign in to your account even if the attempts go
          on.
        </p>
        <Button
          type="button"
          className="w-full"
          loading={unlock.isPending}
          disabled={preview.isPending}
          onClick={() => {
            if (!unlock.isPending) unlock.mutate();
          }}
        >
          Unlock sign-in
        </Button>
      </div>
    </AuthCard>
  );
}
