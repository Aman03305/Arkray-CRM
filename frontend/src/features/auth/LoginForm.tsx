"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { PasswordField, TextField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import { hardNavigate } from "@/lib/browser";
import { safeNextPath } from "@/lib/safe-redirect";

import { authApi, useViewerQuery } from "./api";
import { AuthCard } from "./AuthCard";

const REASONS = new Map<string, { tone: "info" | "success"; text: string }>([
  ["expired", { tone: "info", text: "Your session has ended. Please sign in again." }],
  ["signed-out", { tone: "success", text: "You have been signed out." }],
  ["activated", { tone: "success", text: "Your account is active. Sign in with your new password." }],
  ["reset", { tone: "success", text: "Your password has been changed. Sign in with your new password." }],
]);

export function LoginForm() {
  const params = useSearchParams();
  const next = safeNextPath(params.get("next"));
  const reason = REASONS.get(params.get("reason") ?? "");
  const queryClient = useQueryClient();
  const existingSession = useViewerQuery();
  const form = useRef<HTMLFormElement>(null);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [missing, setMissing] = useState<Record<string, string[]> | null>(null);

  const login = useMutation({
    mutationFn: authApi.login,
    onSuccess: () => {
      setPassword("");
      // A new identity: drop everything cached, tell other tabs, start afresh.
      queryClient.clear();
      hardNavigate(next, "signed-in");
    },
  });

  // Already signed in: carry on, unless this page has something to say (for example a
  // freshly activated account on a browser where someone else is still signed in).
  useEffect(() => {
    if (existingSession.isSuccess && !reason) hardNavigate(next);
  }, [existingSession.isSuccess, next, reason]);

  const serverFields = fieldErrors(login.error);
  useFocusFirstInvalid(form, missing ?? login.error);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (login.isPending) return;
    const problems: Record<string, string[]> = {};
    if (!email.trim()) problems.email = ["Enter your email address."];
    if (!password) problems.password = ["Enter your password."];
    setMissing(Object.keys(problems).length ? problems : null);
    if (Object.keys(problems).length === 0) login.mutate({ email: email.trim(), password });
  };

  // An administrator-set password that was never replaced in time: the server says what to do.
  const temporaryExpired = isApiError(login.error, 400, "temporary_password_expired");
  const errors = temporaryExpired ? { ...missing } : { ...serverFields, ...missing };
  const showBanner = login.isError && (temporaryExpired || Object.keys(serverFields).length === 0);
  const banner = describeError(login.error);
  const otherSession = existingSession.isSuccess && reason ? existingSession.data : null;

  return (
    <AuthCard title="Sign in" subtitle="Use your work email and password.">
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {reason && !login.isError ? <Alert tone={reason.tone}>{reason.text}</Alert> : null}
        {otherSession ? (
          <Alert tone="info">
            This browser is still signed in as {otherSession.fullName}. Signing in below switches
            accounts.
          </Alert>
        ) : null}
        {showBanner ? (
          <Alert tone="error" requestId={isApiError(login.error, 400) ? null : banner.requestId}>
            {banner.message}
          </Alert>
        ) : null}
        <TextField
          label="Email"
          name="email"
          type="email"
          autoComplete="username"
          inputMode="email"
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          errors={errors.email}
        />
        <PasswordField
          label="Password"
          name="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          errors={errors.password}
        />
        <div className="flex justify-end">
          <Link href="/forgot-password" prefetch={false} className="text-sm font-medium text-brand-700 hover:underline">
            Forgot password?
          </Link>
        </div>
        <Button type="submit" className="w-full" loading={login.isPending || login.isSuccess}>
          Sign in
        </Button>
      </form>
    </AuthCard>
  );
}
