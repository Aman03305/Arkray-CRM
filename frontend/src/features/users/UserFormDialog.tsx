"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { SelectField, TextField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { AdminUser, Role, UserUpdateRequest } from "@/lib/api/types";

import { ROLE_OPTIONS, USERS_QUERY_KEY, usersApi } from "./api";

type Mode = { kind: "create" } | { kind: "edit"; user: AdminUser; isSelf: boolean };

interface Draft {
  firstName: string;
  lastName: string;
  email: string;
  role: Role;
}

function initialDraft(mode: Mode): Draft {
  return mode.kind === "edit"
    ? { firstName: mode.user.first_name, lastName: mode.user.last_name, email: mode.user.email, role: mode.user.role }
    : { firstName: "", lastName: "", email: "", role: "sales_user" };
}

function changes(user: AdminUser, draft: Draft): UserUpdateRequest {
  const body: { first_name?: string; last_name?: string; role?: Role; version: number } = { version: user.version };
  if (draft.firstName.trim() !== user.first_name) body.first_name = draft.firstName.trim();
  if (draft.lastName.trim() !== user.last_name) body.last_name = draft.lastName.trim();
  if (draft.role !== user.role) body.role = draft.role;
  return body;
}

/**
 * Create a user (who receives an emailed invitation; nobody chooses their password) or
 * edit names and role. Email changes are a separate, explicit operation.
 */
export function UserFormDialog({ mode, onClose, onSaved }: {
  mode: Mode;
  onClose: () => void;
  onSaved: (user: AdminUser, message: string) => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => initialDraft(mode));
  const [clientErrors, setClientErrors] = useState<Record<string, string[]>>({});
  const form = useRef<HTMLFormElement>(null);
  const queryClient = useQueryClient();
  const save = useMutation({
    mutationFn: () =>
      mode.kind === "create"
        ? usersApi.create({
            first_name: draft.firstName.trim(),
            last_name: draft.lastName.trim(),
            email: draft.email.trim(),
            role: draft.role,
          })
        : usersApi.update(mode.user.id, changes(mode.user, draft)),
    onSuccess: (user) =>
      onSaved(
        user,
        mode.kind === "create"
          ? `${user.full_name} was created. An invitation is on its way to ${user.email}.`
          : `${user.full_name} was updated.`,
      ),
    onError: (error) => {
      // Stale version: refresh the list so reopening the dialog starts from current data.
      if (isApiError(error, 409) && mode.kind === "edit") {
        void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
      }
    },
  });

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    if (save.isPending) return;
    const problems: Record<string, string[]> = {};
    if (!draft.firstName.trim()) problems.first_name = ["Enter a first name."];
    if (mode.kind === "create" && !draft.email.trim()) problems.email = ["Enter an email address."];
    setClientErrors(problems);
    if (Object.keys(problems).length === 0) save.mutate();
  };

  const server = fieldErrors(save.error);
  const errors = { ...server, ...clientErrors };
  useFocusFirstInvalid(form, Object.keys(clientErrors).length ? clientErrors : save.error);
  const stale = isApiError(save.error, 409) && mode.kind === "edit";
  const knownFields = ["first_name", "last_name", "email", "role"];
  const showBanner = save.isError && !knownFields.some((f) => server[f]?.length);
  const banner = describeError(save.error);
  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));
  const title = mode.kind === "create" ? "New user" : `Edit ${mode.user.full_name}`;

  return (
    <Dialog
      open
      title={title}
      description={mode.kind === "create" ? "They'll receive an email invitation to set their own password." : undefined}
      onClose={onClose}
      busy={save.isPending}
    >
      <form ref={form} onSubmit={onSubmit} noValidate className="space-y-4">
        {showBanner ? (
          <Alert tone="error" requestId={stale ? null : banner.requestId}>
            {stale
              ? "Someone else changed this user while you were editing. Close this dialog and reopen it to edit the latest details."
              : (server.non_field_errors?.join(" ") ?? banner.message)}
          </Alert>
        ) : null}
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField
            label="First name"
            name="first_name"
            autoComplete="off"
            data-autofocus
            maxLength={100}
            value={draft.firstName}
            onChange={(e) => set({ firstName: e.target.value })}
            errors={errors.first_name}
          />
          <TextField
            label="Last name"
            name="last_name"
            autoComplete="off"
            optional
            maxLength={100}
            value={draft.lastName}
            onChange={(e) => set({ lastName: e.target.value })}
            errors={errors.last_name}
          />
        </div>
        {mode.kind === "create" ? (
          <TextField
            label="Email"
            name="email"
            type="email"
            autoComplete="off"
            inputMode="email"
            maxLength={254}
            value={draft.email}
            onChange={(e) => set({ email: e.target.value })}
            errors={errors.email}
            hint="This is how they'll sign in."
          />
        ) : null}
        <SelectField
          label="Role"
          name="role"
          value={draft.role}
          onChange={(e) => set({ role: e.target.value as Role })}
          options={ROLE_OPTIONS}
          disabled={mode.kind === "edit" && mode.isSelf}
          errors={errors.role}
          hint={
            mode.kind === "edit" && mode.isSelf
              ? "You can't change your own role."
              : ROLE_OPTIONS.find((o) => o.value === draft.role)?.description
          }
        />
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={save.isPending}>
            {mode.kind === "create" ? "Create and invite" : "Save changes"}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
