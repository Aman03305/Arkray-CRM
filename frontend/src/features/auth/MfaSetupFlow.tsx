"use client";

import { useMutation } from "@tanstack/react-query";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { PasswordField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { MfaSetup } from "@/lib/api/types";

import { mfaApi } from "./api";
import { FactorField, factorProblem, normalizeCode, serverCodeErrors } from "./FactorField";
import { QrCode } from "./QrCode";
import { RecoveryCodes } from "./RecoveryCodes";

/** The set-up key in groups of four, as authenticator apps ask for it ("JBSW Y3DP ..."). */
export function groupSecret(secret: string): string {
  return (secret.replace(/\s+/g, "").match(/.{1,4}/g) ?? []).join(" ");
}

function PasswordStep({ onStarted, onCancel, notice }: {
  onStarted: (setup: MfaSetup) => void;
  onCancel?: () => void;
  notice: string | null;
}) {
  const [password, setPassword] = useState("");
  const [missing, setMissing] = useState<string | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const start = useMutation({
    mutationFn: () => mfaApi.setup(password),
    gcTime: 0, // the request held the password
    onSuccess: (setup) => {
      setPassword("");
      onStarted(setup);
    },
  });
  useFocusFirstInvalid(form, missing ?? start.error);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (start.isPending) return;
    const problem = password ? null : "Enter your current password.";
    setMissing(problem);
    if (!problem) start.mutate();
  };

  const server = fieldErrors(start.error).current_password;
  const errors = missing ? [missing] : server;
  const banner = start.isError && !server ? describeError(start.error, { tooMany: "attempts" }) : null;

  return (
    <form ref={form} onSubmit={onSubmit} noValidate className="max-w-sm space-y-4">
      {notice && !start.isError ? <Alert tone="info">{notice}</Alert> : null}
      {banner ? (
        <Alert tone="error" requestId={isApiError(start.error, 409) ? null : banner.requestId}>
          {banner.message}
        </Alert>
      ) : null}
      <p className="text-sm text-slate-600">
        You&apos;ll need an authenticator app on your phone, such as Google Authenticator, Microsoft Authenticator or
        1Password. First, confirm it&apos;s you.
      </p>
      <PasswordField
        label="Current password"
        name="current-password"
        autoComplete="current-password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        errors={errors}
      />
      <div className="flex flex-wrap gap-2">
        <Button type="submit" loading={start.isPending}>
          Continue
        </Button>
        {onCancel ? (
          <Button variant="secondary" onClick={onCancel} disabled={start.isPending}>
            Cancel
          </Button>
        ) : null}
      </div>
    </form>
  );
}

function ScanStep({ setup, onConfirmed, onRestart, onCancel }: {
  setup: MfaSetup;
  onConfirmed: (codes: readonly string[]) => void;
  onRestart: (message: string) => void;
  onCancel?: () => void;
}) {
  const [code, setCode] = useState("");
  const [missing, setMissing] = useState<string | null>(null);
  const form = useRef<HTMLFormElement>(null);
  // It replaces the password form, whose button is gone: start reading here.
  const intro = useRef<HTMLParagraphElement>(null);
  useEffect(() => intro.current?.focus(), []);
  const confirm = useMutation({
    mutationFn: () => mfaApi.confirm(normalizeCode(code)),
    gcTime: 0, // the answer holds the recovery codes
    onSuccess: (answer) => {
      setCode("");
      onConfirmed(answer.recovery_codes);
    },
    onError: (error) => {
      // The pending set-up is gone (expired, or replaced in another tab): start again.
      if (isApiError(error, 400, "mfa_setup_required")) onRestart(error.message);
    },
  });
  useFocusFirstInvalid(form, missing ?? confirm.error);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (confirm.isPending) return;
    const problem = factorProblem("code", code);
    setMissing(problem);
    if (!problem) confirm.mutate();
  };

  const server = serverCodeErrors(confirm.error);
  const errors = missing ? [missing] : server;
  const banner = confirm.isError && !server ? describeError(confirm.error, { tooMany: "attempts" }) : null;

  return (
    <form ref={form} onSubmit={onSubmit} noValidate className="@container space-y-4">
      {banner ? (
        <Alert tone="error" requestId={banner.requestId}>
          {banner.message}
        </Alert>
      ) : null}
      <p ref={intro} tabIndex={-1} className="text-sm font-medium text-slate-900 focus:outline-none">
        Add Arkray CRM to your authenticator app
      </p>
      <ol className="list-decimal space-y-1 pl-5 text-sm text-slate-600">
        <li>Open your authenticator app and add an account.</li>
        <li>Scan this QR code, or enter the key below.</li>
        <li>Enter the 6-digit code the app shows.</li>
      </ol>
      {/* Side by side only where the form is wide enough (Settings, not the sign-in card). */}
      <div className="flex flex-col gap-4 @md:flex-row @md:items-start">
        <div className="shrink-0 self-start rounded-md border border-slate-200 bg-white p-2">
          <QrCode qr={setup.qr} label="QR code to add Arkray CRM to your authenticator app" />
        </div>
        <div className="min-w-0 space-y-1 text-sm">
          <p className="font-medium text-slate-700">Can&apos;t scan it? Enter this key:</p>
          <p className="font-mono text-base tracking-wide text-slate-900 [overflow-wrap:anywhere]">
            <span className="sr-only">Set-up key: </span>
            {groupSecret(setup.secret)}
          </p>
          <p className="text-xs text-slate-500">Choose a time-based key if the app asks. This key isn&apos;t shown again.</p>
        </div>
      </div>
      <div className="max-w-sm">
        <FactorField kind="code" value={code} onChange={setCode} errors={errors} />
      </div>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" loading={confirm.isPending}>
          Turn on two-step verification
        </Button>
        {onCancel ? (
          <Button variant="secondary" onClick={onCancel} disabled={confirm.isPending}>
            Cancel
          </Button>
        ) : null}
      </div>
    </form>
  );
}

type Step = { kind: "password"; notice: string | null } | { kind: "scan"; setup: MfaSetup } | { kind: "codes"; codes: readonly string[] };

/**
 * Setting up two-step verification (ADR-0033): the current password, then the QR code (or
 * its key) and a first code from the app, which turns it on; then the recovery codes, once.
 * The secret and the codes live only in this component's state. `onComplete` runs once the
 * user has said the codes are saved (the caller refreshes the viewer then, not earlier, so
 * the codes stay on screen). Used by Settings and by the enforced set-up at sign-in.
 */
export function MfaSetupFlow({ onComplete, onCancel }: { onComplete: () => void; onCancel?: () => void }) {
  const [step, setStep] = useState<Step>({ kind: "password", notice: null });
  if (step.kind === "codes") return <RecoveryCodes codes={step.codes} onDone={onComplete} doneLabel="Done" />;
  if (step.kind === "scan") {
    return (
      <ScanStep
        setup={step.setup}
        onConfirmed={(codes) => setStep({ kind: "codes", codes })}
        onRestart={(message) => setStep({ kind: "password", notice: message })}
        onCancel={onCancel}
      />
    );
  }
  return <PasswordStep notice={step.notice} onStarted={(setup) => setStep({ kind: "scan", setup })} onCancel={onCancel} />;
}
