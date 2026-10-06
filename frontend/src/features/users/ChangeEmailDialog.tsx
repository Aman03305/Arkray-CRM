"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { PasswordField, TextField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";

import { USERS_QUERY_KEY, usersApi } from "./api";

/** Email is the sign-in identity, so changing it is its own, clearly explained action. */
export function ChangeEmailDialog({ user, isSelf, onClose, onSaved }: {
  user: AdminUser;
  isSelf: boolean;
  onClose: () => void;
  onSaved: (user: AdminUser, message: string) => void;
}) {
  const [email, setEmail] = useState(user.email);
  const [currentPassword, setCurrentPassword] = useState("");
  const [missing, setMissing] = useState<Record<string, string[]>>({});
  const form = useRef<HTMLFormElement>(null);
  const queryClient = useQueryClient();
  const save = useMutation({
    mutationFn: () =>
      usersApi.changeEmail(user.id, {
        email: email.trim(),
        version: user.version,
        ...(isSelf ? { current_password: currentPassword } : {}),
      }),
    onSuccess: (updated) => onSaved(updated, `${updated.full_name} now signs in with ${updated.email}.`),
    onError: (error) => {
      // A stale version, or a refusal (e.g. the user became an administrator meanwhile).
      if ((isApiError(error, 409) && !fieldErrors(error).email) || isApiError(error, 422)) {
        void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
      }
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    const problems: Record<string, string[]> = {};
    if (!email.trim()) problems.email = ["Enter an email address."];
    if (isSelf && !currentPassword) problems.current_password = ["Enter your current password."];
    setMissing(problems);
    if (Object.keys(problems).length === 0) save.mutate();
  };

  const fields = { ...fieldErrors(save.error), ...missing };
  useFocusFirstInvalid(form, Object.keys(missing).length ? missing : save.error);
  const banner =
    save.isError && !fields.email && !fields.current_password ? describeError(save.error) : null;
  const consequence =
    user.status === "invited"
      ? "Their current invitation link stops working and a new invitation is sent to the new address."
      : "They'll be signed out everywhere and must sign in with the new address. The old address is notified.";

  return (
    <Dialog open title={`Change email for ${user.full_name}`} description={consequence} onClose={onClose} busy={save.isPending}>
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {banner ? (
          <Alert tone="error" requestId={banner.requestId}>
            {banner.message}
          </Alert>
        ) : null}
        <TextField
          label="New email"
          name="email"
          type="email"
          inputMode="email"
          autoComplete="off"
          data-autofocus
          maxLength={254}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          errors={fields.email}
        />
        {isSelf ? (
          <PasswordField
            label="Your current password"
            name="current-password"
            autoComplete="current-password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            errors={fields.current_password}
            hint="Required to change your own sign-in email."
          />
        ) : null}
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={save.isPending}>
            Change email
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
