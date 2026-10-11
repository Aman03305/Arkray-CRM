"use client";

import { useMutation } from "@tanstack/react-query";
import { type FormEvent, type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { registerStepUpHandler } from "@/lib/api/client";
import { describeError } from "@/lib/api/errors";

import { mfaApi } from "./api";
import { FactorField, type FactorKind, factorBody, factorProblem, serverCodeErrors } from "./FactorField";

/**
 * Asks for a fresh second factor whenever the API answers 403 `mfa_required` (ADR-0033: user
 * administration, support sessions, privacy requests need one from the last 15 minutes).
 * apiFetch calls the handler registered here, waits, and repeats the refused request once
 * (lib/api/client), so no screen needs code of its own for it. One dialog serves every
 * request refused at the same time.
 */
export function StepUpProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  // Everyone waiting for the answer (apiFetch shares one prompt, so normally one).
  const waiting = useRef<((verified: boolean) => void)[]>([]);

  const settle = useCallback((verified: boolean) => {
    const resolvers = waiting.current;
    waiting.current = [];
    resolvers.forEach((resolve) => resolve(verified));
    setOpen(false);
  }, []);

  useEffect(() => {
    const unregister = registerStepUpHandler(
      () =>
        new Promise<boolean>((resolve) => {
          waiting.current.push(resolve);
          setOpen(true);
        }),
    );
    return () => {
      unregister();
      // Leaving the app (sign-out, another identity): whatever waited is cancelled.
      const resolvers = waiting.current;
      waiting.current = [];
      resolvers.forEach((resolve) => resolve(false));
    };
  }, []);

  return (
    <>
      {children}
      {open ? <StepUpDialog onVerified={() => settle(true)} onCancel={() => settle(false)} /> : null}
    </>
  );
}

/** The prompt itself. The code lives only in this dialog's state (and the request's body). */
function StepUpDialog({ onVerified, onCancel }: { onVerified: () => void; onCancel: () => void }) {
  const [kind, setKind] = useState<FactorKind>("code");
  // The dialog focuses the field when it opens (and remembers what had focus before, to
  // return it there); only a field swapped in afterwards focuses itself.
  const [switched, setSwitched] = useState(false);
  const [value, setValue] = useState("");
  const [missing, setMissing] = useState<string | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const verify = useMutation({
    // No mutation variables: the code isn't kept in the mutation cache.
    mutationFn: () => mfaApi.verify(factorBody(kind, value)),
    gcTime: 0,
    onSuccess: () => {
      setValue("");
      onVerified();
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (verify.isPending) return;
    const problem = factorProblem(kind, value);
    setMissing(problem);
    if (!problem) verify.mutate();
  };

  // Focus the field again after a refusal (useFocusFirstInvalid's job in page forms).
  useEffect(() => {
    if (missing || verify.error) form.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
  }, [missing, verify.error]);

  const switchKind = () => {
    setKind((k) => (k === "code" ? "recovery" : "code"));
    setSwitched(true);
    setValue("");
    setMissing(null);
    verify.reset();
  };

  const errors = missing ? [missing] : serverCodeErrors(verify.error, kind === "code" ? "code" : "recovery_code");
  const banner = verify.isError && !errors ? describeError(verify.error, { tooMany: "attempts" }) : null;

  return (
    <Dialog
      open
      title="Confirm it's you"
      description={
        kind === "code"
          ? "This change needs a recent second step. Enter the code from your authenticator app."
          : "This change needs a recent second step. Enter one of your recovery codes."
      }
      onClose={onCancel}
      busy={verify.isPending}
      size="sm"
    >
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {banner ? (
          <Alert tone="error" requestId={banner.requestId}>
            {banner.message}
          </Alert>
        ) : null}
        <FactorField key={kind} kind={kind} value={value} onChange={setValue} errors={errors} dataAutofocus autoFocus={switched} />
        <button
          type="button"
          onClick={switchKind}
          disabled={verify.isPending}
          className="rounded-sm text-sm font-medium text-brand-700 hover:underline disabled:opacity-50"
        >
          {kind === "code" ? "Use a recovery code instead" : "Use your authenticator app instead"}
        </button>
        <DialogActions>
          <Button variant="secondary" onClick={onCancel} disabled={verify.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={verify.isPending}>
            Verify
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
