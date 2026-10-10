"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { TextField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { SECURITY_EVENTS_QUERY_KEY, USERS_QUERY_KEY } from "@/features/users/api";
import { workspaceKeys } from "@/features/workspace/api";
import { describeError, fieldErrors } from "@/lib/api/errors";
import type { AdminUser, PseudonymiseResult } from "@/lib/api/types";

import { privacyApi } from "./api";

/** What the server removed, in the order an administrator would ask about it. */
const RESULT_LABELS: [keyof PseudonymiseResult, string][] = [
  ["conversations", "Ask Arkray conversations deleted"],
  ["audit_details", "Expiring audit details deleted"],
  ["support_reasons", "Support session reasons deleted"],
  ["tokens_revoked", "Sign-in and invitation links revoked"],
  ["support_sessions_ended", "Support sessions ended"],
  ["access_windows", "Workspace access records removed"],
  ["throttle_events", "Sign-in attempt records removed"],
  ["queued_payloads", "Queued messages removed"],
];

/** The confirmation is the account's current email, typed out (case aside). */
export function confirmsEmail(typed: string, email: string): boolean {
  return typed.trim().toLowerCase() === email.trim().toLowerCase();
}

/**
 * Pseudonymising a deactivated user who has asked to be forgotten (docs/privacy.md): their
 * name and email are replaced by a pseudonym and their password removed; their Ask Arkray
 * conversations and expiring audit details are deleted. Their records stay attributed (to
 * the pseudonym), so the CRM's history stays whole. Irreversible, so the administrator types
 * the account's email to confirm; the server refuses an active user or one who still owns
 * current work, and says why.
 */
export function PseudonymiseDialog({ user, onClose }: { user: AdminUser; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [typed, setTyped] = useState("");
  const [mismatch, setMismatch] = useState<Record<string, string[]>>({});
  const form = useRef<HTMLFormElement>(null);
  const done = useRef<HTMLDivElement>(null);

  const run = useMutation({
    mutationFn: () => privacyApi.pseudonymise(user.id, typed.trim()),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
      void queryClient.invalidateQueries({ queryKey: SECURITY_EVENTS_QUERY_KEY });
      // Their workspace banner names them.
      void queryClient.invalidateQueries({ queryKey: workspaceKeys.subject(user.id) });
    },
  });
  useEffect(() => {
    if (run.isSuccess) done.current?.querySelector<HTMLElement>("button")?.focus();
  }, [run.isSuccess]);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (run.isPending || run.isSuccess) return;
    const problems: Record<string, string[]> = confirmsEmail(typed, user.email)
      ? {}
      : { confirm_email: [typed.trim() ? "That isn't this account's email address." : "Type the account's email address to confirm."] };
    setMismatch(problems);
    if (Object.keys(problems).length === 0) run.mutate();
  };

  const errors = { ...fieldErrors(run.error), ...mismatch };
  useFocusFirstInvalid(form, Object.keys(mismatch).length ? mismatch : run.error);
  const banner = run.isError && !fieldErrors(run.error).confirm_email?.length ? describeError(run.error) : null;

  return (
    <Dialog
      open
      role="alertdialog"
      title={`Pseudonymise ${user.full_name}?`}
      description={
        <>
          <p>
            This can&apos;t be undone. Their name and email are replaced by a pseudonym and their password is removed; their Ask
            Arkray conversations and expiring audit details are deleted.
          </p>
          <p>Their records stay attributed, to the pseudonym.</p>
        </>
      }
      onClose={onClose}
      busy={run.isPending}
      size="sm"
    >
      {run.isSuccess ? (
        <div ref={done} className="space-y-4">
          <Alert tone="success" title="Account pseudonymised">
            <ul className="mt-1 space-y-0.5">
              {RESULT_LABELS.filter(([key]) => run.data[key] > 0).map(([key, label]) => (
                <li key={key}>
                  {label}: {run.data[key]}
                </li>
              ))}
            </ul>
          </Alert>
          <DialogActions>
            <Button variant="secondary" onClick={onClose}>
              Close
            </Button>
          </DialogActions>
        </div>
      ) : (
        <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
          {banner ? (
            <Alert tone="error" requestId={banner.requestId}>
              {fieldErrors(run.error).non_field_errors?.join(" ") ?? banner.message}
            </Alert>
          ) : null}
          <TextField
            label="Type their email to confirm"
            name="confirm_email"
            type="email"
            inputMode="email"
            autoComplete="off"
            data-autofocus
            spellCheck={false}
            maxLength={254}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            errors={errors.confirm_email}
            hint={user.email}
          />
          <DialogActions>
            <Button variant="secondary" onClick={onClose} disabled={run.isPending}>
              Cancel
            </Button>
            <Button type="submit" variant="danger" loading={run.isPending}>
              Pseudonymise
            </Button>
          </DialogActions>
        </form>
      )}
    </Dialog>
  );
}
