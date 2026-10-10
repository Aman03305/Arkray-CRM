"use client";

import { useMutation } from "@tanstack/react-query";
import { LogOut } from "lucide-react";
import { type FormEvent, type ReactNode, useId, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { PasswordField } from "@/components/ui/Field";
import { PageHeader } from "@/components/ui/PageHeader";
import { Skeleton } from "@/components/ui/Skeleton";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { authApi } from "@/features/auth/api";
import { confirmationErrors, NewPasswordFields, type NewPasswordValue } from "@/features/auth/NewPasswordFields";
import { useSignOut } from "@/features/auth/useSignOut";
import { describeError, fieldErrors } from "@/lib/api/errors";
import { useViewer } from "@/lib/viewer-context";

function Section({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="rounded-lg border border-slate-200 bg-white">
      <div className="border-b border-slate-200 px-5 py-4">
        <h2 id={headingId} className="text-sm font-semibold text-slate-900">
          {title}
        </h2>
        {description ? <p className="mt-0.5 text-sm text-slate-500">{description}</p> : null}
      </div>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

function ChangePasswordForm() {
  const [current, setCurrent] = useState("");
  const [value, setValue] = useState<NewPasswordValue>({ password: "", confirmation: "" });
  const [clientErrors, setClientErrors] = useState<Record<string, string[]>>({});
  const form = useRef<HTMLFormElement>(null);
  const change = useMutation({
    mutationFn: () => authApi.changePassword(current, value.password),
    gcTime: 0, // the request held passwords
    onSuccess: () => {
      setCurrent("");
      setValue({ password: "", confirmation: "" });
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (change.isPending) return;
    const problems: Record<string, string[]> = { ...confirmationErrors(value) };
    if (!current) problems.current = ["Enter your current password."];
    setClientErrors(problems);
    if (Object.keys(problems).length === 0) change.mutate();
  };

  const server = fieldErrors(change.error);
  useFocusFirstInvalid(form, Object.keys(clientErrors).length ? clientErrors : change.error);
  const errors = { current: server.current_password, password: server.new_password, ...clientErrors };
  const banner =
    change.isError && !server.current_password && !server.new_password ? describeError(change.error) : null;

  return (
    <form ref={form} onSubmit={onSubmit} noValidate className="max-w-sm space-y-4">
      {change.isSuccess ? (
        <Alert tone="success">Password changed. You&apos;ve been signed out on your other devices.</Alert>
      ) : null}
      {banner ? (
        <Alert tone="error" requestId={banner.requestId}>
          {banner.message}
        </Alert>
      ) : null}
      <PasswordField
        label="Current password"
        name="current-password"
        autoComplete="current-password"
        value={current}
        onChange={(e) => setCurrent(e.target.value)}
        errors={errors.current}
      />
      <NewPasswordFields value={value} onChange={setValue} errors={errors} />
      <Button type="submit" loading={change.isPending}>
        Change password
      </Button>
    </form>
  );
}

export function ProfilePage() {
  const viewer = useViewer();
  const signOut = useSignOut();
  const signOutError = signOut.isError ? describeError(signOut.error) : null;

  const rows: [string, string | undefined][] = [
    ["Name", viewer?.fullName],
    ["Email", viewer?.email],
    ["Role", viewer?.roleLabel],
  ];

  return (
    <>
      <PageHeader title="Settings" />
      <div className="space-y-6">
        <Section title="Profile" description="An administrator changes your name or email.">
          <dl className="grid gap-4 sm:grid-cols-3">
            {rows.map(([label, value]) => (
              <div key={label}>
                <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</dt>
                <dd className="mt-1 text-sm text-slate-900">
                  {value ?? (
                    <>
                      <Skeleton className="h-4 w-32" />
                      <span className="sr-only">Loading</span>
                    </>
                  )}
                </dd>
              </div>
            ))}
          </dl>
        </Section>

        <Section title="Password" description="Changing it signs you out on your other devices.">
          <ChangePasswordForm />
        </Section>

        <Section title="Session" description="Ends after 2 hours idle.">
          {signOutError ? (
            <div className="mb-3">
              <Alert tone="error" requestId={signOutError.requestId}>
                {signOutError.message}
              </Alert>
            </div>
          ) : null}
          <Button
            variant="secondary"
            icon={<LogOut aria-hidden="true" className="size-4" />}
            loading={signOut.isPending || signOut.isSuccess}
            onClick={() => signOut.mutate()}
          >
            Sign out
          </Button>
        </Section>
      </div>
    </>
  );
}
