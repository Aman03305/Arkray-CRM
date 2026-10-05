"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { PasswordField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, fieldErrors } from "@/lib/api/errors";
import { VIEWER_QUERY_KEY } from "@/lib/query-client";
import type { Viewer } from "@/lib/viewer";

import { authApi } from "./api";
import { AuthCard } from "./AuthCard";
import { confirmationErrors, NewPasswordFields, type NewPasswordValue } from "./NewPasswordFields";
import { useSignOut } from "./useSignOut";

const EMPTY: NewPasswordValue = { password: "", confirmation: "" };

/**
 * Shown instead of the app while an administrator-set password must be replaced: the API
 * refuses everything else meanwhile (403 password_change_required). Once changed, the
 * viewer is fetched again and the app opens.
 */
export function ForcedPasswordChange({ viewer }: { viewer: Viewer }) {
  const queryClient = useQueryClient();
  const signOut = useSignOut();
  const [current, setCurrent] = useState("");
  const [value, setValue] = useState<NewPasswordValue>(EMPTY);
  const [clientErrors, setClientErrors] = useState<Record<string, string[]>>({});
  const form = useRef<HTMLFormElement>(null);
  const change = useMutation({
    mutationFn: () => authApi.changePassword(current, value.password),
    gcTime: 0, // the request held passwords
    onSuccess: async () => {
      setCurrent("");
      setValue(EMPTY);
      await queryClient.invalidateQueries({ queryKey: VIEWER_QUERY_KEY });
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (change.isPending) return;
    const problems: Record<string, string[]> = { ...confirmationErrors(value) };
    if (!current) problems.current = ["Enter the temporary password."];
    setClientErrors(problems);
    if (Object.keys(problems).length === 0) change.mutate();
  };

  const server = fieldErrors(change.error);
  useFocusFirstInvalid(form, Object.keys(clientErrors).length ? clientErrors : change.error);
  const errors = { current: server.current_password, password: server.new_password, ...clientErrors };
  const banner = change.isError && !server.current_password && !server.new_password ? describeError(change.error) : null;
  const signOutError = signOut.isError ? describeError(signOut.error) : null;

  return (
    <AuthCard
      title="Choose a new password"
      subtitle="Replace the temporary password to continue."
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
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {banner ? (
          <Alert tone="error" requestId={banner.requestId}>
            {banner.message}
          </Alert>
        ) : null}
        {signOutError ? (
          <Alert tone="error" requestId={signOutError.requestId}>
            {signOutError.message}
          </Alert>
        ) : null}
        {/* For password managers: whose password this is. */}
        <input type="email" name="username" autoComplete="username" value={viewer.email} readOnly hidden />
        <PasswordField
          label="Temporary password"
          name="current-password"
          autoComplete="current-password"
          autoFocus
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          errors={errors.current}
        />
        <NewPasswordFields value={value} onChange={setValue} errors={errors} />
        <Button type="submit" className="w-full" loading={change.isPending}>
          Change password
        </Button>
      </form>
    </AuthCard>
  );
}
