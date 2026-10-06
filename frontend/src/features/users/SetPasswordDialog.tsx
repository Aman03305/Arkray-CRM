"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { confirmationErrors, NewPasswordFields, type NewPasswordValue } from "@/features/auth/NewPasswordFields";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";

import { USERS_QUERY_KEY, usersApi } from "./api";

const EMPTY: NewPasswordValue = { password: "", confirmation: "" };

/**
 * An administrator sets a new password for a user who can't sign in (forgotten, no email).
 * The user is signed out everywhere and must replace it at their next sign-in. The
 * password exists only in these two fields until it is sent; it is never shown afterwards.
 */
export function SetPasswordDialog({ user, onClose, onDone }: {
  user: AdminUser;
  onClose: () => void;
  onDone: (user: AdminUser, message: string) => void;
}) {
  const [value, setValue] = useState<NewPasswordValue>(EMPTY);
  const [clientErrors, setClientErrors] = useState<Record<string, string[]>>({});
  const form = useRef<HTMLFormElement>(null);
  const queryClient = useQueryClient();
  const save = useMutation({
    mutationFn: () => usersApi.setPassword(user.id, { version: user.version, new_password: value.password }),
    gcTime: 0, // the request held a password: don't keep it around once the dialog is gone
    onSuccess: (updated) => {
      setValue(EMPTY);
      onDone(updated, `${updated.full_name} must choose a new password at next sign-in.`);
    },
    onError: (error) => {
      // A stale version, or a refusal (e.g. the user became an administrator meanwhile).
      if (isApiError(error, 409) || isApiError(error, 422)) void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    const problems = confirmationErrors(value);
    setClientErrors(problems);
    if (Object.keys(problems).length === 0) save.mutate();
  };

  const server = fieldErrors(save.error);
  const errors = { password: server.new_password, ...clientErrors };
  useFocusFirstInvalid(form, Object.keys(clientErrors).length ? clientErrors : save.error);
  const stale = isApiError(save.error, 409);
  const banner = save.isError && !server.new_password ? describeError(save.error) : null;

  return (
    <Dialog
      open
      title={`Set a new password for ${user.full_name}`}
      description="They'll be signed out and must choose their own password at next sign-in."
      onClose={onClose}
      busy={save.isPending}
    >
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {banner ? (
          <Alert tone="error" requestId={stale ? null : banner.requestId}>
            {stale ? "Someone else changed this user meanwhile. Close this dialog and try again." : banner.message}
          </Alert>
        ) : null}
        <NewPasswordFields value={value} onChange={setValue} errors={errors} autoComplete="off" />
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={save.isPending}>
            Set password
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
