"use client";

import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, isApiError } from "@/lib/api/errors";

import { authApi } from "./api";
import { AuthCard } from "./AuthCard";
import {
  FactorField,
  type FactorKind,
  factorBody,
  factorProblem,
  isMfaLocked,
  MFA_LOCKED_MESSAGE,
  serverCodeErrors,
} from "./FactorField";

const SUBTITLES: Record<FactorKind, string> = {
  code: "Enter the code from your authenticator app to finish signing in.",
  recovery: "Enter one of the recovery codes you saved when you set up two-step verification.",
};

/**
 * The second step of signing in (ADR-0033): the password was right, and the account has
 * two-step verification. Nothing is signed in yet; the server holds a short-lived challenge.
 * `onRestart` goes back to the email and password (Back, or the challenge expired, with the
 * server's explanation).
 */
export function LoginMfaStep({ onSignedIn, onRestart }: { onSignedIn: () => void; onRestart: (message?: string) => void }) {
  const [kind, setKind] = useState<FactorKind>("code");
  const [value, setValue] = useState("");
  const [missing, setMissing] = useState<string | null>(null);
  const form = useRef<HTMLFormElement>(null);

  const verify = useMutation({
    mutationFn: authApi.loginMfa,
    gcTime: 0, // its variables are the code: not kept once the form is gone
    onSuccess: () => {
      setValue("");
      onSignedIn();
    },
    onError: (error) => {
      // Too slow, too many wrong codes, or the password changed meanwhile: start again.
      if (isApiError(error, 400, "mfa_challenge_expired")) onRestart(error.message);
      // Locked by wrong codes over many sign-ins: the challenge is over too, and the way out
      // (Forgot password) is on the first step.
      else if (isMfaLocked(error)) onRestart(MFA_LOCKED_MESSAGE);
    },
  });

  useFocusFirstInvalid(form, missing ?? verify.error);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (verify.isPending || verify.isSuccess) return;
    const problem = factorProblem(kind, value);
    setMissing(problem);
    if (!problem) verify.mutate(factorBody(kind, value));
  };

  const switchKind = () => {
    setKind((k) => (k === "code" ? "recovery" : "code"));
    setValue("");
    setMissing(null);
    verify.reset();
  };

  const fieldErrors = missing ? [missing] : serverCodeErrors(verify.error, kind === "code" ? "code" : "recovery_code");
  const banner = verify.isError && !fieldErrors ? describeError(verify.error, { tooMany: "sign-in attempts" }) : null;

  return (
    <AuthCard
      title="Two-step verification"
      subtitle={SUBTITLES[kind]}
      footer={
        <button
          type="button"
          onClick={() => onRestart()}
          disabled={verify.isPending || verify.isSuccess}
          className="rounded-sm font-medium text-brand-700 hover:underline disabled:opacity-50"
        >
          Back to sign in
        </button>
      }
    >
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {banner ? (
          <Alert tone="error" requestId={isApiError(verify.error, 400) ? null : banner.requestId}>
            {banner.message}
          </Alert>
        ) : null}
        <FactorField key={kind} kind={kind} value={value} onChange={setValue} errors={fieldErrors} autoFocus />
        <Button type="submit" className="w-full" loading={verify.isPending || verify.isSuccess}>
          Verify
        </Button>
        <div className="flex justify-center">
          <button
            type="button"
            onClick={switchKind}
            disabled={verify.isPending || verify.isSuccess}
            className="rounded-sm text-sm font-medium text-brand-700 hover:underline disabled:opacity-50"
          >
            {kind === "code" ? "Use a recovery code instead" : "Use your authenticator app instead"}
          </button>
        </div>
      </form>
    </AuthCard>
  );
}
