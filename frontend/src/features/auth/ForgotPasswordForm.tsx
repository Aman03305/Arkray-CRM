"use client";

import { useMutation } from "@tanstack/react-query";
import Link from "next/link";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { TextField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, fieldErrors } from "@/lib/api/errors";

import { authApi } from "./api";
import { AuthCard } from "./AuthCard";

export function ForgotPasswordForm() {
  const [email, setEmail] = useState("");
  const [missing, setMissing] = useState<string[] | undefined>();
  const request = useMutation({ mutationFn: authApi.requestPasswordReset });
  const form = useRef<HTMLFormElement>(null);
  useFocusFirstInvalid(form, missing ?? request.error);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (request.isPending) return;
    if (!email.trim()) {
      setMissing(["Enter your email address."]);
      return;
    }
    setMissing(undefined);
    request.mutate(email.trim());
  };

  const backToSignIn = (
    <Link href="/login" className="font-medium text-brand-700 hover:underline">
      Back to sign in
    </Link>
  );

  if (request.isSuccess) {
    return (
      <AuthCard title="Check your email" footer={backToSignIn}>
        {/* The same words whether or not an account exists (no account enumeration). */}
        <Alert tone="success">{request.data.detail}</Alert>
        <p className="mt-4 text-sm text-slate-500">
          The link works once and expires after an hour. If nothing arrives, check your spam
          folder or ask your administrator.
        </p>
      </AuthCard>
    );
  }

  const error = describeError(request.error);
  const fields = fieldErrors(request.error);
  return (
    <AuthCard
      title="Reset your password"
      subtitle="Enter your work email and we'll send you a link to choose a new password."
      footer={backToSignIn}
    >
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {request.isError && !fields.email ? (
          <Alert tone="error" requestId={error.requestId}>
            {error.message}
          </Alert>
        ) : null}
        <TextField
          label="Email"
          name="email"
          type="email"
          autoComplete="email"
          inputMode="email"
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          errors={missing ?? fields.email}
        />
        <Button type="submit" className="w-full" loading={request.isPending}>
          Send reset link
        </Button>
      </form>
    </AuthCard>
  );
}
