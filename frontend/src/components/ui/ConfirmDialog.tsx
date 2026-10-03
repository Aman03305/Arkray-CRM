"use client";

import type { ReactNode } from "react";

import { Alert } from "./Alert";
import { Button } from "./Button";
import { Dialog, DialogActions } from "./Dialog";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** The consequences; announced with the title (it is the dialog's description). */
  children: ReactNode;
  confirmLabel: string;
  tone?: "primary" | "danger";
  busy?: boolean;
  error?: { message: string; requestId: string | null } | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Explicit confirmation for security-sensitive actions (deactivate, re-invite, ...). An
 * `alertdialog` whose consequences are read out; for destructive actions focus starts on
 * Cancel, so a stray Enter can't confirm.
 */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  tone = "primary",
  busy = false,
  error,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const danger = tone === "danger";
  return (
    <Dialog open={open} title={title} description={children} onClose={onCancel} busy={busy} size="sm" role="alertdialog">
      {error ? (
        <Alert tone="error" requestId={error.requestId}>
          {error.message}
        </Alert>
      ) : null}
      <DialogActions>
        <Button variant="secondary" onClick={onCancel} disabled={busy} data-autofocus={danger || undefined}>
          Cancel
        </Button>
        <Button variant={tone} onClick={onConfirm} loading={busy} data-autofocus={danger ? undefined : true}>
          {confirmLabel}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
