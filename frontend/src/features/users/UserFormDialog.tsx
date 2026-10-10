"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogActions } from "@/components/ui/Dialog";
import { PasswordField, SelectField, TextField } from "@/components/ui/Field";
import { useFocusFirstInvalid } from "@/components/ui/useFocusFirstInvalid";
import { describeError, fieldErrors, isApiError } from "@/lib/api/errors";
import type { AdminUser, Role, UserCreateRequest, UserUpdateRequest } from "@/lib/api/types";

import { ROLE_OPTIONS, USERS_QUERY_KEY, usersApi } from "./api";
import { generatePassword } from "./password";

type Mode = { kind: "create" } | { kind: "edit"; user: AdminUser; isSelf: boolean };

/** How a new user gets in: an emailed invitation (the default), or a password the
 * administrator sets now. */
type Activation = "password" | "invite";

const MIN_PASSWORD_LENGTH = 12;

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

const LINK_BUTTON = "rounded-sm text-sm font-medium text-brand-700 hover:underline disabled:opacity-50";

/**
 * Create a user, or edit names and role (email changes are a separate, explicit operation).
 * A new user gets an emailed invitation to choose their own password (the default: no one
 * else ever knows it) or, if preferred, an initial password from the administrator (they
 * must replace it when they first sign in). That password lives only in this form: it is
 * cleared once the user exists and is never shown again.
 */
export function UserFormDialog({ mode, onClose, onSaved }: {
  mode: Mode;
  onClose: () => void;
  onSaved: (user: AdminUser, message: string) => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => initialDraft(mode));
  const [activation, setActivation] = useState<Activation>("invite");
  const [password, setPassword] = useState("");
  const [generated, setGenerated] = useState(false);
  const [clientErrors, setClientErrors] = useState<Record<string, string[]>>({});
  const form = useRef<HTMLFormElement>(null);
  const queryClient = useQueryClient();
  // Administrators always choose their own password (the server refuses one set for them).
  const invitedOnly = mode.kind === "create" && draft.role === "admin";
  // Your own role, and another administrator's, can't be changed here (the API refuses both).
  const roleLocked = mode.kind === "edit" && (mode.isSelf || mode.user.role === "admin");
  const withPassword = mode.kind === "create" && activation === "password" && !invitedOnly;

  const save = useMutation({
    mutationFn: () => {
      if (mode.kind === "edit") return usersApi.update(mode.user.id, changes(mode.user, draft));
      const body: UserCreateRequest = {
        first_name: draft.firstName.trim(),
        last_name: draft.lastName.trim(),
        email: draft.email.trim(),
        role: draft.role,
        ...(withPassword ? { password } : {}),
      };
      return usersApi.create(body);
    },
    // Not kept in the mutation cache once this dialog is gone (the request held a password).
    gcTime: 0,
    onSuccess: (user) => {
      setPassword("");
      onSaved(
        user,
        mode.kind === "edit"
          ? `${user.full_name} was updated.`
          : withPassword
            ? `${user.full_name} can sign in now. They'll choose their own password at first sign-in.`
            : `${user.full_name} was created. An invitation is on its way to ${user.email}.`,
      );
    },
    onError: (error) => {
      // Stale version, or a refusal a stale page couldn't foresee (the user became an
      // administrator meanwhile): refresh the list so reopening starts from current data.
      if ((isApiError(error, 409) || isApiError(error, 422)) && mode.kind === "edit") {
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
    if (withPassword && !password) problems.password = ["Enter a password, or email an invitation instead."];
    else if (withPassword && password.length < MIN_PASSWORD_LENGTH) {
      problems.password = [`Use at least ${MIN_PASSWORD_LENGTH} characters.`];
    }
    setClientErrors(problems);
    if (Object.keys(problems).length === 0) save.mutate();
  };

  const switchActivation = () => {
    setActivation((a) => (a === "password" ? "invite" : "password"));
    setPassword("");
    setGenerated(false);
    save.reset(); // a refused password says nothing about an invitation
    // Drop a password-only complaint (other complaints stay, without moving focus).
    if (clientErrors.password && Object.keys(clientErrors).length === 1) setClientErrors({});
  };

  const server = fieldErrors(save.error);
  const errors = { ...server, ...clientErrors };
  useFocusFirstInvalid(form, Object.keys(clientErrors).length ? clientErrors : save.error);
  const stale = isApiError(save.error, 409) && mode.kind === "edit";
  const knownFields = ["first_name", "last_name", "email", "role", ...(withPassword ? ["password"] : [])];
  const showBanner = save.isError && !knownFields.some((f) => server[f]?.length);
  const banner = describeError(save.error);
  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));
  const title = mode.kind === "create" ? "New user" : `Edit ${mode.user.full_name}`;

  return (
    <Dialog open title={title} onClose={onClose} busy={save.isPending}>
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
            hint="Used to sign in."
          />
        ) : null}
        <SelectField
          label="Role"
          name="role"
          value={draft.role}
          onChange={(e) => set({ role: e.target.value as Role })}
          options={ROLE_OPTIONS}
          disabled={roleLocked}
          errors={errors.role}
          hint={
            mode.kind === "edit" && mode.isSelf
              ? "You can't change your own role."
              : roleLocked
                ? "An administrator's role can't be changed here. To remove their access, deactivate the account."
                : ROLE_OPTIONS.find((o) => o.value === draft.role)?.description
          }
        />
        {invitedOnly ? (
          <p className="rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-600">
            Administrators are invited: we&apos;ll email them a link to set their own password.
          </p>
        ) : mode.kind === "create" ? (
          <div>
            {activation === "password" ? (
              <PasswordField
                label="Initial password"
                name="initial-password"
                // Someone else's password: the browser must not save it as the admin's own.
                autoComplete="off"
                data-1p-ignore
                data-lpignore="true"
                maxLength={128}
                value={password}
                onChange={(e) => {
                  setPassword(e.target.value);
                  setGenerated(false);
                }}
                errors={errors.password}
                hint="They'll choose their own at first sign-in."
              />
            ) : (
              <p className="rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-600">
                We&apos;ll email them a link to set their own password.
              </p>
            )}
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
              {activation === "password" ? (
                <button
                  type="button"
                  className={LINK_BUTTON}
                  disabled={save.isPending}
                  onClick={() => {
                    setPassword(generatePassword());
                    setGenerated(true);
                  }}
                >
                  Generate password
                </button>
              ) : null}
              <button type="button" className={LINK_BUTTON} onClick={switchActivation} disabled={save.isPending}>
                {activation === "password" ? "Email an invitation instead" : "Set a password instead"}
              </button>
            </div>
            <p aria-live="polite" className="sr-only">
              {generated ? "Password generated. Use Show to see it." : ""}
            </p>
          </div>
        ) : null}
        <DialogActions>
          <Button variant="secondary" onClick={onClose} disabled={save.isPending}>
            Cancel
          </Button>
          <Button type="submit" loading={save.isPending}>
            {mode.kind === "edit" ? "Save changes" : withPassword ? "Create user" : "Create and invite"}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  );
}
