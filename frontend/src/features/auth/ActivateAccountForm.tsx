"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import { hardNavigate } from "@/lib/browser";

import { authApi } from "./api";
import { AuthCard } from "./AuthCard";
import { confirmationErrors, NewPasswordFields, type NewPasswordValue } from "./NewPasswordFields";

function InvalidInvitation({ message }: { message: string }) {
  return (
    <AuthCard
      title="This invitation can't be used"
      footer={
        // No prefetch: a prefetch request would carry this page's URL (and its one-time
        // secret) in a header.
        <Link href="/login" prefetch={false} className="font-medium text-brand-700 hover:underline">
          Go to sign in
        </Link>
      }
    >
      <Alert tone="error">{message}</Alert>
      <p className="mt-4 text-sm text-slate-500">
        Invitation links work once and expire after 72 hours; a newer invitation replaces an
        older one. Ask your Arkray CRM administrator to send you a new invitation.
      </p>
    </AuthCard>
  );
}

export function ActivateAccountForm({ token }: { token: string }) {
  const invitation = useQuery({
    queryKey: ["invitation", token],
    queryFn: () => authApi.verifyInvitation(token),
    retry: false,
    staleTime: Infinity,
  });
  const [value, setValue] = useState<NewPasswordValue>({ password: "", confirmation: "" });
  const [clientErrors, setClientErrors] = useState<Record<string, string[]> | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const accept = useMutation({
    mutationFn: (password: string) => authApi.acceptInvitation(token, password),
    gcTime: 0, // its variable is the new password: not kept once the form is gone
    onSuccess: () => hardNavigate("/login?reason=activated"),
  });
  useFocusFirstInvalid(form, clientErrors ?? accept.error);

  if (invitation.isPending) {
    return (
      <AuthCard title="Checking your invitation">
        <p role="status" className="flex items-center gap-2 text-sm text-slate-500">
          <Spinner /> One moment…
        </p>
      </AuthCard>
    );
  }
  const invalid = [invitation.error, accept.error].find((e) => isApiError(e, 400, "invalid_token"));
  if (invalid) return <InvalidInvitation message={(invalid as Error).message} />;
  if (invitation.isError) {
    // Anything else (rate limit, outage, network) says nothing about the link itself.
    const { message, requestId } = describeError(invitation.error);
    return (
      <AuthCard title="We couldn't check your invitation">
        <Alert
          tone="error"
          requestId={requestId}
          action={
            <Button variant="secondary" size="sm" onClick={() => void invitation.refetch()} loading={invitation.isFetching}>
              Try again
            </Button>
          }
        >
          {message}
        </Alert>
      </AuthCard>
    );
  }

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (accept.isPending) return;
    const problems = confirmationErrors(value);
    setClientErrors(Object.keys(problems).length ? problems : null);
    if (Object.keys(problems).length === 0) accept.mutate(value.password);
  };

  const server = fieldErrors(accept.error);
  const errors = { password: server.password, ...clientErrors };
  const banner = accept.isError && !server.password ? describeError(accept.error) : null;

  return (
    <AuthCard
      title={`Welcome, ${invitation.data.first_name}`}
      subtitle={
        <>
          Set a password for <span className="font-medium text-slate-700">{invitation.data.email}</span> to
          activate your account.
        </>
      }
    >
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {banner ? (
          <Alert tone="error" requestId={banner.requestId}>
            {banner.message}
          </Alert>
        ) : null}
        <NewPasswordFields value={value} onChange={setValue} errors={errors} label="Password" />
        <Button type="submit" className="w-full" loading={accept.isPending || accept.isSuccess}>
          Activate account
        </Button>
      </form>
    </AuthCard>
  );
}
