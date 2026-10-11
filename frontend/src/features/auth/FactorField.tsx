"use client";

import { TextField } from "@/components/ui/Field";
import { fieldErrors, isApiError } from "@/lib/api/errors";
import type { MfaFactorRequest } from "@/lib/api/types";

/** What proves the second step: a code from the authenticator app, or a recovery code. */
export type FactorKind = "code" | "recovery";

export const CODE_LENGTH = 6;

/** Digits only: apps show "123 456", and a pasted code may carry spaces or a dash. */
export function normalizeCode(value: string): string {
  return value.replace(/\D/g, "");
}

/** Recovery codes are sent as typed, without surrounding or inner spaces (the server
 * compares them in its own canonical form). */
export function normalizeRecoveryCode(value: string): string {
  return value.replace(/\s+/g, "");
}

/** The client-side check before anything is sent; null when it can be sent. */
export function factorProblem(kind: FactorKind, value: string): string | null {
  if (kind === "recovery") return normalizeRecoveryCode(value) ? null : "Enter one of your recovery codes.";
  return normalizeCode(value).length === CODE_LENGTH ? null : `Enter the ${CODE_LENGTH}-digit code from your authenticator app.`;
}

export function factorBody(kind: FactorKind, value: string): MfaFactorRequest {
  return kind === "recovery" ? { recovery_code: normalizeRecoveryCode(value) } : { code: normalizeCode(value) };
}

/**
 * The server's refusal of a code, for the code field: its validation details for the field,
 * or the message of a wrong code (400 `invalid_mfa_code`). Undefined for anything else
 * (shown as a banner instead).
 */
export function serverCodeErrors(error: unknown, field: "code" | "recovery_code" = "code"): string[] | undefined {
  const own = fieldErrors(error)[field];
  if (own?.length) return own;
  if (isApiError(error, 400, "invalid_mfa_code")) return [error.message || "That code didn't work. Try again."];
  return undefined;
}

interface FactorFieldProps {
  kind: FactorKind;
  value: string;
  onChange: (value: string) => void;
  errors?: readonly string[];
  /** Focus on mount (sign-in step). In a dialog, use `dataAutofocus` instead. */
  autoFocus?: boolean;
  dataAutofocus?: boolean;
  /** Overrides the default label ("Authentication code" / "Recovery code"). */
  label?: string;
  disabled?: boolean;
}

/** The code input, set up for one-time codes (autofill from SMS-less authenticators,
 * numeric keypad on phones) or for a recovery code. */
export function FactorField({ kind, value, onChange, errors, autoFocus, dataAutofocus, label, disabled }: FactorFieldProps) {
  if (kind === "recovery") {
    return (
      <TextField
        label={label ?? "Recovery code"}
        name="recovery_code"
        autoComplete="off"
        autoCapitalize="characters"
        spellCheck={false}
        placeholder="XXXX-XXXX-XXXX-XXXX"
        hint="Each recovery code works once."
        autoFocus={autoFocus}
        data-autofocus={dataAutofocus || undefined}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        errors={errors}
        disabled={disabled}
      />
    );
  }
  return (
    <TextField
      label={label ?? "Authentication code"}
      name="code"
      inputMode="numeric"
      autoComplete="one-time-code"
      maxLength={9}
      spellCheck={false}
      hint={`The ${CODE_LENGTH}-digit code your authenticator app shows for Arkray CRM.`}
      autoFocus={autoFocus}
      data-autofocus={dataAutofocus || undefined}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      errors={errors}
      disabled={disabled}
    />
  );
}
