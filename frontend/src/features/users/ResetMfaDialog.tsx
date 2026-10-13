"use client";

import { useMutation } from "@tanstack/react-query";

import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { describeError } from "@/lib/api/errors";
import type { AdminUser } from "@/lib/api/types";

import { usersApi } from "./api";
import { useOutOfReachOn404 } from "./reach";

/**
 * Removing an ordinary user's two-step verification, for someone who lost their phone and
 * their recovery codes (ADR-0033). Audited by the server; their sessions end. Never offered
 * for administrators (the API refuses it: an operator resets theirs on the server).
 */
export function ResetMfaDialog({ user, onClose, onDone }: {
  user: AdminUser;
  onClose: () => void;
  onDone: (user: AdminUser, message: string) => void;
}) {
  const reset = useMutation({
    mutationFn: () => usersApi.resetMfa(user.id),
    onSuccess: (updated) => onDone(updated, `Two-step verification was reset for ${updated.full_name}.`),
  });
  // Out of reach now (404): the Users page closes this and says why (ADR-0035).
  useOutOfReachOn404(reset.error, user);
  return (
    <ConfirmDialog
      open
      title={`Reset two-step verification for ${user.full_name}?`}
      confirmLabel="Reset two-step verification"
      tone="danger"
      busy={reset.isPending}
      error={reset.isError ? describeError(reset.error) : null}
      onConfirm={() => reset.mutate()}
      onCancel={onClose}
    >
      <p>Their authenticator app and recovery codes stop working, and they&apos;re signed out everywhere.</p>
      <p>Only do this once you&apos;ve confirmed who is asking. They can set it up again from Settings.</p>
    </ConfirmDialog>
  );
}
