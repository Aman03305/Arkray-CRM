"use client";

import { useMutation } from "@tanstack/react-query";
import Link from "next/link";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import { hardNavigate } from "@/lib/browser";

import { authApi } from "./api";
import { AuthCard } from "./AuthCard";
import { confirmationErrors, NewPasswordFields, type NewPasswordValue } from "./NewPasswordFields";

export function ResetPasswordForm({ token }: { token: string }) {
  const [value, setValue] = useState<NewPasswordValue>({ password: "", confirmation: "" });
  const [clientErrors, setClientErrors] = useState<Record<string, string[]> | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const reset = useMutation({
    mutationFn: (password: string) => authApi.confirmPasswordReset(token, password),
    gcTime: 0, // its variable is the new password: not kept once the form is gone
    // Every session was ended by the reset: sign in again with the new password.
    onSuccess: () => hardNavigate("/login?reason=reset"),
  });
  useFocusFirstInvalid(form, clientErrors ?? reset.error);

  if (isApiError(reset.error, 400, "invalid_token")) {
    return (
      <AuthCard
        title="This link can't be used"
        footer={
          <Link href="/login" prefetch={false} className="font-medium text-brand-700 hover:underline">
            Back to sign in
          </Link>
        }
      >
        <Alert tone="error">{reset.error.message}</Alert>
        <p className="mt-4 text-sm text-slate-500">
          Reset links work once and expire after an hour, and a newer request replaces an older
          link.
        </p>
        <Link
          href="/forgot-password"
          prefetch={false}
          className="mt-4 inline-flex h-9 w-full items-center justify-center rounded-full bg-brand-600 text-sm font-medium text-white hover:bg-brand-700"
        >
          Request a new link
        </Link>
      </AuthCard>
    );
  }

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (reset.isPending) return;
    const problems = confirmationErrors(value);
    setClientErrors(Object.keys(problems).length ? problems : null);
    if (Object.keys(problems).length === 0) reset.mutate(value.password);
  };

  const server = fieldErrors(reset.error);
  const errors = { password: server.new_password, ...clientErrors };
  const banner = reset.isError && !server.new_password ? describeError(reset.error) : null;

  return (
    <AuthCard title="Choose a new password" subtitle="You'll be signed out everywhere and can then sign in with it.">
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {banner ? (
          <Alert tone="error" requestId={banner.requestId}>
            {banner.message}
          </Alert>
        ) : null}
        <NewPasswordFields value={value} onChange={setValue} errors={errors} />
        <Button type="submit" className="w-full" loading={reset.isPending || reset.isSuccess}>
          Change password
        </Button>
      </form>
    </AuthCard>
  );
}
