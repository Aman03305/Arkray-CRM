"use client";

import { PasswordField } from "@/components/ui/Field";

import { PASSWORD_POLICY_HINT } from "./AuthCard";

export interface NewPasswordValue {
  password: string;
  confirmation: string;
}

/** Client-side checks only for what the server can't know (the two fields match). */
export function confirmationErrors(value: NewPasswordValue): Record<string, string[]> {
  if (!value.password) return { password: ["Choose a password."] };
  if (value.password !== value.confirmation) return { confirmation: ["The passwords don't match."] };
  return {};
}

export function NewPasswordFields({
  value,
  onChange,
  errors,
  label = "New password",
  autoComplete = "new-password",
}: {
  value: NewPasswordValue;
  onChange: (value: NewPasswordValue) => void;
  errors: Record<string, readonly string[] | undefined>;
  label?: string;
  /** "off" when the password is someone else's (an administrator setting it), so the
   * browser doesn't offer to save it as the administrator's own. */
  autoComplete?: "new-password" | "off";
}) {
  const other = autoComplete === "off" ? { "data-1p-ignore": true, "data-lpignore": "true" } : {};
  return (
    <>
      <PasswordField
        label={label}
        name="new-password"
        autoComplete={autoComplete}
        {...other}
        value={value.password}
        onChange={(e) => onChange({ ...value, password: e.target.value })}
        errors={errors.password}
        hint={PASSWORD_POLICY_HINT}
      />
      <PasswordField
        label="Confirm password"
        name="confirm-password"
        autoComplete={autoComplete}
        {...other}
        value={value.confirmation}
        onChange={(e) => onChange({ ...value, confirmation: e.target.value })}
        errors={errors.confirmation}
      />
    </>
  );
}
